import torch
from torch.optim import AdamW
from transformers import get_scheduler # TODO: Come on now, implement this yourself

from minRLHF.actor import Actor
from minRLHF.critic import Critic
from minRLHF.buffer import Buffer
from minRLHF.environment import Environment
from minRLHF.utils import gather_dict

class PPOTrainer:
    def __init__(
        self, 
        actor_model, 
        critic_model,
        reference_model, 
        env: Environment,
        max_ep_length: int = 100,
        rollout_batch_size: int = 32,
        rollout_batches_per_epoch: int = 4,
        num_epochs: int = 1000,
        actor_train_batch_size: int = 16,
        actor_train_iters: int = 4,
        actor_lr: float = 1e-5,
        critic_train_batch_size: int = 16,
        critic_train_iters: int = 8,
        critic_lr: float = 1e-5,
        target_kl: float = 0.05,
        clip_ratio: float = 0.2,
        gamma: float = 1.0,
        lam: float = 1.0,
        beta: float = 0.005,
        save_steps: int = 50,
        log_steps: int = 1,
        log_smoothing_val: float = 0.95
    ):
        """Constructor for PPOTrainer class.
        
        Args:
            actor (Model): Needs to implement generate and forward.
            
            critic (Model): Needs to forward method.
            
            env (minRLHF.environment.Environment): Env used to generate initial
                                                   prompts and score the actor's generations.
        """
        pad_token_id = 50256       
        # save hyperparams
        self.max_ep_length = max_ep_length
        self.rollout_batch_size = rollout_batch_size
        self.rollout_batches_per_epoch = rollout_batches_per_epoch
        self.num_epochs = num_epochs
        self.actor_train_batch_size = actor_train_batch_size
        self.actor_train_iters = actor_train_iters
        self.actor_lr = actor_lr
        self.critic_train_batch_size = critic_train_batch_size
        self.critic_train_iters = critic_train_iters
        self.critic_lr = critic_lr
        self.target_kl = target_kl
        self.clip_ratio = clip_ratio
        self.gamma = gamma
        self.lam = lam
        self.beta = beta
        self.save_steps = save_steps
        self.log_steps = log_steps
        self.log_smoothing_val = log_smoothing_val
        
        # Setup actor and optimizer
        self.actor = Actor(actor_model, pad_token_id=pad_token_id, generation_max_length=self.max_ep_length)
        self.actor_optimizer = AdamW(self.actor.model.parameters(), lr=self.actor_lr)
        self.actor_lr_scheduler = get_scheduler(
            'linear',
            optimizer=self.actor_optimizer,
            num_warmup_steps=0,
            num_training_steps=self.num_epochs
        )
        
        # Setup critic and optimizer
        self.critic = Critic(critic_model)
        self.critic_optimizer = AdamW(self.critic.model.parameters(), lr=self.critic_lr)
        self.critic_lr_scheduler = get_scheduler(
            'linear',
            optimizer=self.critic_optimizer,
            num_warmup_steps=0,
            num_training_steps=self.num_epochs
        )
        
        # Other setup
        self.rolling_average_logging_vals = {}
        self.collected_rewards = []
        self.reference = Actor(reference_model, pad_token_id=pad_token_id, generation_max_length=self.max_ep_length) 
        self.env = env
        def naive_logprob_augmenter(buf: Buffer)->None:
            buf.reward_augmentation_buffer[:, :] = -((buf.pi_t_logprobs_buffer - buf.pi_0_logprobs_buffer) ** 2)/2
        self.buffer = Buffer(
            max_episodes=self.rollout_batch_size * self.rollout_batches_per_epoch,
            max_ep_length=self.max_ep_length,
            reward_augmenter=naive_logprob_augmenter,
            device=torch.device('cpu')
        ) # TODO: Might need to do this at train time
    
        
    def compute_actor_loss(self, data):
        data = gather_dict(data, self.actor.device, keys=[
            'ids', 'prompt_mask', 'completion_mask', 'advantages', 'pi_0_logprobs', 'pi_t_logprobs'
        ])

        # compute logprobs with gradients for most recent policy
        logprobs, pi = self.actor.get_logits(data['ids'], data['prompt_mask'], data['completion_mask'])
        # mask and flatten into sequence of decisions
        masked_logprobs = logprobs.masked_select(data['completion_mask'].to(torch.bool))
        masked_reference_logprobs = data['pi_t_logprobs'].masked_select(data['completion_mask'].to(torch.bool))
        masked_advantages = data['advantages'].masked_select(data['completion_mask'].to(torch.bool))
        
        # compute ppo loss
        ratio = torch.exp(masked_logprobs - masked_reference_logprobs)
        clip_adv = torch.clamp(ratio, 1-self.clip_ratio, 1+self.clip_ratio) * masked_advantages
        loss = -(torch.min(ratio * masked_advantages, clip_adv)).mean()
        
        # useful metrics for logging
        kld_t = (masked_logprobs - masked_reference_logprobs).mean().item()
        masked_logprobs_0 = data['pi_0_logprobs'].masked_select(data['completion_mask'].to(torch.bool))
        kld_0 = (masked_logprobs - masked_logprobs_0).mean().item()
        entropy = pi.entropy().masked_select(data['completion_mask'].to(torch.bool)).mean().item()
        
        return loss, {
            'kld_t-1': kld_t,
            'kld_0': kld_0,
            'entropy': entropy
        }
    
    
    def compute_critic_loss(self, data):
        # Perform computations on critic device
        data = gather_dict(data, self.critic.device, keys=[
            'ids', 'prompt_mask', 'completion_mask', 'critic_targets'
        ])
        
        # recompute logits with gradient (buffer stores with no grad)
        train_logits = self.critic.get_value_estimates(
            data['ids'],
            data['prompt_mask'],
            data['completion_mask']
        ).masked_select(data['completion_mask'].to(torch.bool))

        train_targets = data['critic_targets'].masked_select(data['completion_mask'].to(torch.bool))
        
        loss = ((train_logits - train_targets)**2).mean()
        mae = (train_logits - train_targets).abs().mean()
        
        return loss, {'mae': mae.item()}
    
    
    def get_rollout(self):  # ppo采样方式，每一个timestep依据概率采样
        with torch.no_grad():
            data = {}   # we store working variables in here for easier device management
            
            data['prompt_ids'], data['prompt_mask'] = self.env.reset()
            
            # Completions and associated logprobs computed on actor device
            data = gather_dict(data, self.actor.device, keys=['prompt_ids', 'prompt_mask'])
            data['completion_ids'], data['completion_mask'] = self.actor.get_rollouts(data['prompt_ids'], data['prompt_mask'])
            data['pi_t_logprobs'], _ = self.actor.get_logits(data['completion_ids'], data['prompt_mask'], data['completion_mask'])
            
            # Reference logprobs computed on reference device
            data = gather_dict(data, self.reference.device, keys=['completion_ids', 'prompt_mask', 'completion_mask'])
            data['pi_0_logprobs'], _ = self.reference.get_logits(data['completion_ids'], data['prompt_mask'], data['completion_mask'])
            
            # Rewards computed by environment on cpu
            data = gather_dict(data, torch.device('cpu'), keys=['completion_ids', 'prompt_mask', 'completion_mask'])
            data['reward'] = self.env.get_rewards(data['completion_ids'], data['prompt_mask'], data['completion_mask']) # 稀疏奖励 [B, T]
            
            # Compute critic value estimates on critic device
            data = gather_dict(data, self.critic.device, keys=['completion_ids', 'prompt_mask', 'completion_mask'])
            data['value_estimates'] = self.critic.get_value_estimates(data['completion_ids'], data['prompt_mask'], data['completion_mask'])
            
            # Our buffer will only store (batch, seq) arrays so we need to pad prompt mask
            pad_length = data['completion_mask'].shape[1] - data['prompt_mask'].shape[1]
            data['prompt_mask'] = torch.nn.functional.pad(data['prompt_mask'], (0,pad_length))
            
            # Do some key mangling to make the return dict map to our buffer correctly
            data.pop('prompt_ids')  # 因为prompt ids已经存在completion ids里面了，把他当做state即可
            data['state'] = data.pop('completion_ids')
            
            return data
            
        
        
    def train(self):
        
        for epoch in range(self.num_epochs):
            
            self.buffer.reset()
            
            # Generate rollout_batches_per_epoch * rollout_batch_size rollouts
            for rollout_batch_idx in range(self.rollout_batches_per_epoch):
                # print(f'Generating rollout batch {rollout_batch_idx}')
                rollout = self.get_rollout()
                rollout = gather_dict(rollout, self.buffer.device)
                self.buffer.store(**rollout)
                
            
            # Use the rollouts to optimise the actor
            for actor_train_step in range(self.actor_train_iters):
                actor_train_batches = self.buffer.get(self.actor_train_batch_size, self.gamma, self.lam, self.beta)
                
                average_actor_loss_info = {}
                for batch_idx, buf_data in enumerate(actor_train_batches):
                    # print(f'Getting actor loss for train step {actor_train_step} and batch {batch_idx}')
                    actor_loss, actor_loss_info = self.compute_actor_loss(buf_data)
                    actor_loss.backward()
                    
                    # average the loss info over the batches
                    for k,v in actor_loss_info.items():
                        if k in average_actor_loss_info:
                            average_actor_loss_info[k].append(v)
                        else:
                            average_actor_loss_info[k] = [v]
                            
                for k,v in average_actor_loss_info.items():
                    average_actor_loss_info[k] = sum(v)/len(v)
                    
                if average_actor_loss_info['kld_t-1'] > 1.5 * self.target_kl:
                    print(f'(Epoch:{epoch} actor iter: {actor_train_step}) Early stopping due to kl of ~', average_actor_loss_info['kld_t-1'])
                    break
                    
                self.actor_optimizer.step()
                self.actor_optimizer.zero_grad()
                
            self.actor_lr_scheduler.step()
                
            # Use the rollouts to optimise the critic
            for critic_train_step in range(self.critic_train_iters):
                critic_train_batches = self.buffer.get(self.critic_train_batch_size, self.gamma, self.lam, self.beta)
                
                average_critic_loss_info = {}
                for batch_idx, buf_data in enumerate(critic_train_batches):
                    # print(f'Getting critic loss for step {critic_train_step} and batch {batch_idx}')
                    critic_loss, critic_loss_info = self.compute_critic_loss(buf_data)
                    critic_loss.backward()
                    
                    # average the loss info over the batches
                    for k,v in average_critic_loss_info.items():
                        if k in average_critic_loss_info:
                            average_critic_loss_info[k].append(v)
                        else:
                            average_critic_loss_info[k] = [v]    
                            
                for k,v in average_critic_loss_info.items():
                    average_critic_loss_info[k] = sum(v)/len(v)
                    
                self.critic_optimizer.step()
                self.critic_optimizer.zero_grad()
                
            self.critic_lr_scheduler.step()
                
            # Logging
            self.collected_rewards.append(self.buffer.summary()['reward_mean'])
            self.log(self.buffer.summary())
            self.log(average_actor_loss_info)
            self.log(average_critic_loss_info)
            
            if (epoch + 1)%self.log_steps == 0:
                print(f'Completed epoch {epoch}.')
                for k, v in self.rolling_average_logging_vals.items():
                    print(f'{k}: {v}')
                print()

            if (epoch + 1)%self.save_steps == 0:
                model_fpath = f'actor_{epoch}.model'
                self.actor.model.save_pretrained(model_fpath)
                print(f'(Epoch: {epoch}) Saved model to {model_fpath}')
                
                
    def log(self, data: dict):
        for k, v in data.items():
            # Update rolling average
            if k in self.rolling_average_logging_vals:
                self.rolling_average_logging_vals[k] = ((1 - self.log_smoothing_val) * v) + (self.log_smoothing_val * self.rolling_average_logging_vals[k])
            else:
                self.rolling_average_logging_vals[k] = v
            