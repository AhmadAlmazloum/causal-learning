import torch
import torch.optim as optim
from framework.train.utils.schedulers import LinearDecayLR, CyclicLR

LR_CYCLE_SIZE = 20000
STEPS_100K = 100000  # Represents the number of steps over which decay is applied

class OptimizationManager:
    def __init__(self, networks, learning_params, total_iterations):
        self.total_iterations = total_iterations

        self.optimizers = []
        self.schedulers = []
        self.clip_grad_ranges = []
        self.max_grad_norms = []
        self.current_lrs = []
        self.initial_lrs = []

        self.setup_optimization(networks, learning_params)
        self.__networks = networks

    def setup_optimization(self, networks, learning_params):
        for network, params in zip(networks, learning_params):
            optimizer = self.create_optimizer(network, params['lr'])
            scheduler = self.create_scheduler(optimizer, params, self.total_iterations)
            
            self.current_lrs.append(params['lr'])
            self.initial_lrs.append(params['lr'])
            self.clip_grad_ranges.append(params['clip_grad_range'])
            self.max_grad_norms.append(params['max_grad_norm'])
            self.optimizers.append(optimizer)
            self.schedulers.append(scheduler)

    def create_optimizer(self, network, lr):
        return optim.Adam(network.parameters(), lr=lr, betas=(0.9, 0.999))

    def create_scheduler(self, optimizer, params, total_iterations):
        scheduler_type = params['scheduler_type']
        decay_rate = params['decay_rate_100k']
        if scheduler_type == 'linear':
            return LinearDecayLR(optimizer, total_steps=total_iterations, decay_rate_100k=decay_rate)
        elif scheduler_type == 'exponential':
            gamma = pow(decay_rate, 1/STEPS_100K)
            return optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=gamma)
        elif scheduler_type == 'cyclic':
            base_lr = params['lr'] * decay_rate * STEPS_100K / total_iterations
            max_lr = params['lr']
            return CyclicLR(optimizer, base_lr=base_lr, max_lr=max_lr, step_size_up=STEPS_100K // 2, mode='triangular', cycle_momentum=False)
        else:
            raise ValueError(f"Unknown scheduler type: {scheduler_type}")

    def get_lr(self):
        # Sum all the current learning rates from self.current_lr
        tot_lr = sum(self.current_lrs)
        # Calculate the average learning rate; use max to avoid division by zero
        avg_lr = tot_lr / max(len(self.current_lrs), 1)
        return avg_lr
    
    def clip_gradients(self):
        for idx, net in enumerate(self.__networks):
            # Check if net is an instance of LearnableTD
            clip_grad_range = self.clip_grad_ranges[idx]
            max_grad_norm = self.max_grad_norms[idx]
            # Handling for other network types
            if max_grad_norm is not None:
                torch.nn.utils.clip_grad_norm_(net.parameters(), max_grad_norm)
            if clip_grad_range is not None:
                torch.nn.utils.clip_grad_value_(net.parameters(), clip_grad_range)

    def update_optimizers(self):
        for opt in self.optimizers:
            opt.step()
            opt.zero_grad()
                
    def update_schedulers(self):
        for sc in self.schedulers:
            sc.step()
            