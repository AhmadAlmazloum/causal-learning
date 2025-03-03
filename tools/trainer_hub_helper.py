import time
import wandb
import numpy as np
from tools.wandb_logger import wandb_init 
from tools.loader import save_trainer
from tools.logger import log_train_data, log_test_results
from tools.print import print_iter, print_lr, print_trainer, print_test_results
from tools.wandb_logger import wandb_log_train_data, wandb_log_eval_data
from tools.image_debugger import ImageDebugger
from tools.logger import get_log_name
import os
import logging
from tools.tensor_utils import adjust_tensor_dim, generate_padding_mask
from tools.metric_tracker import MetricsTracker
import torch

from torch.utils.tensorboard import SummaryWriter

DEFAULT_SAVE_INTERVAL = 1000

class TrainerHubHelper:
    def __init__(self, parent, data_config, ml_params, device, use_print, use_wandb, print_interval):
        self.parent = parent
        self.device = device
        
        self.use_print = use_print
        self.tensorboard = SummaryWriter(log_dir=get_log_name('../logs'))
        self.data_config = data_config
        if use_wandb:
            wandb_init(data_config, ml_params)
        
        self.use_wandb = use_wandb
        
        self.use_image = len(self.data_config.obs_shape) != 1
        
        self.core_ccnet = self.parent.core_ccnet
        self.core_trainer = self.parent.core_trainer
        
        self.encoder_ccnet = self.parent.encoder_ccnet
        self.encoder_trainer = self.parent.encoder_trainer
        
        self.num_checkpoints = print_interval
        self.save_interval = DEFAULT_SAVE_INTERVAL
        
        self.model_path, self.temp_path, self.log_path = self.setup_directories()
        
        self.logger = logging.getLogger(__name__)
        self.pivot_time = None
        
        self.use_gpt = self.parent.use_gpt
        self.use_core = self.parent.use_core
        self.use_encoder = self.parent.use_encoder
        self.core_metrics = MetricsTracker()
        self.encoder_metrics = MetricsTracker() 
        
        self.use_image_debugger = data_config.show_image_indices is not None
        
        if self.use_image_debugger:
            image_ccnet = self.core_ccnet if self.use_core else self.encoder_ccnet 
            self.image_debugger = ImageDebugger(image_ccnet, self.data_config, self.device, use_core = self.use_core)
        
    def initialize_train(self, dataset):
        # self.sum_losses, self.sum_errors = None, None
        self.iters, self.cnt_checkpoints, self.cnt_print = 0, 0, 0
        if self.use_image_debugger:
            self.image_debugger.initialize_(dataset)

    def should_save_model(self):
        return self.cnt_checkpoints % self.save_interval == 0 and self.cnt_checkpoints != 0
    
    def should_checkpoint(self):
        return self.cnt_checkpoints % self.num_checkpoints == 0 and self.cnt_checkpoints != 0
    
    def init_time_step(self):
        if self.pivot_time is None:
            self.pivot_time = time.time()

    def setup_directories(self, base_path = '../'):
        set_model_path = os.path.join(base_path, "models")
        set_temp_path = os.path.join(base_path, "models/temp")
        set_log_path = os.path.join(base_path, "logs")

        for path in [set_model_path, set_temp_path, set_log_path]:
            os.makedirs(path, exist_ok=True)

        return set_model_path, set_temp_path, set_log_path
            
    def determine_save_path(self):
        """Determine the file path for saving models based on the current count."""
        return self.model_path if self.cnt_print % 2 == 0 else self.temp_path
            
    def prepare_batch_data(self, source_batch, target_batch, padding_mask = None):
        """
        Prepares batch data by adjusting tensor dimensions for processing.

        Parameters:
        - source_batch (Tensor): The source batch tensor.
        - target_batch (Tensor): The target batch tensor.
        - padding_mask (Tensor, optional): The padding mask tensor.

        Returns:
        - state_trajectory (Tensor): The prepared source batch tensor.
        - target_trajectory (Tensor): The prepared target batch tensor.
        - padding_mask (Tensor, optional): The prepared padding mask tensor.
        """
                
        if self.use_gpt:
            # Adjust tensor dimensions for causal processing
            state_trajectory = adjust_tensor_dim(source_batch, target_dim=3)  # off when it's img data set
            target_trajectory = adjust_tensor_dim(target_batch, target_dim=3)  # off when it's img data set
            padding_mask = adjust_tensor_dim(padding_mask, target_dim=3)  # off when it's img data set
        else:
            state_trajectory = source_batch
            target_trajectory = target_batch
        
        return state_trajectory, target_trajectory, padding_mask

    def encode_inputs(self, observation, labels, padding_mask = None):
        with torch.no_grad():
            encoded_obseartion = observation if self.encoder_ccnet is None else self.encoder_ccnet.encode(observation, padding_mask = padding_mask)
        return encoded_obseartion, labels

    def finalize_training_step(self, epoch_idx, iter_idx, len_dataloader, core_metric=None, encoder_metric=None, test_results=None) -> None:
        """Perform end-of-step operations including metrics update and checkpointing."""
        self.update_metrics(core_metric, encoder_metric)
        
        if self.should_checkpoint():
            self.perform_checkpoint_operations(epoch_idx, iter_idx, len_dataloader, test_results)

        self.increment_counters()

    def update_metrics(self, core_metric = None, encoder_metric = None):
        """Update training metrics if available."""
        if core_metric is not None:
            self.core_metrics += core_metric
        if encoder_metric is not None:
            self.encoder_metrics += encoder_metric

    def perform_checkpoint_operations(self, epoch_idx, iter_idx, len_dataloader, test_results=None):
        """Handle all operations required at checkpoint: logging, saving, and metrics reset."""
        time_cost = time.time() - self.pivot_time
        wb_image = self.update_image()
        
        self.log_checkpoint_details(time_cost, epoch_idx, iter_idx, len_dataloader, wb_image)
        self.save_trainers()
        self.reset_metrics()

        if self.use_core and test_results is not None:
            self.handle_test_results(test_results)
            if self.use_wandb:
                wandb_log_eval_data(test_results, wb_image)
                
    def handle_test_results(self, test_results=None):
        """Print and log test results if core is used."""
        print_test_results(test_results)
        log_test_results(self.tensorboard, self.iters, test_results)

    def increment_counters(self):
        """Increment iteration and checkpoint counters."""
        self.iters += 1
        self.cnt_checkpoints += 1

    def update_image(self):
        if self.use_image_debugger:
            # Update and display images when image data is enabled
            self.image_debugger.update_images()
            image_display = self.image_debugger.display_image()
            
            # Log the image to Wandb if enabled
            if self.use_wandb:
                return wandb.Image(image_display)
            
        # Return None if images are not used or logging is not enabled
        return None
    
    def save_trainers(self):
        save_path = self.determine_save_path()
        """Saves the current state of the trainers."""
        if self.use_core:
            save_trainer(save_path, self.core_trainer)
        if self.use_encoder:
            save_trainer(save_path, self.encoder_trainer)
            
    def log_checkpoint_details(self, time_cost, epoch_idx, iter_idx, len_dataloader, wb_image):
        """Calculates average metrics over the checkpoints."""
        avg_core_metric = self.core_metrics / float(self.num_checkpoints) if self.use_core else None
        avg_encoder_metric = self.encoder_metrics / float(self.num_checkpoints) if self.use_encoder else None
        
        if self.use_print:
            self.print_checkpoint_info(time_cost, epoch_idx, iter_idx, len_dataloader, avg_core_metric, avg_encoder_metric)

        log_train_data(self.tensorboard, self.iters, avg_core_metric)
        """Logs training data to Weights & Biases if enabled."""
        if self.use_wandb:
            lr = self.core_trainer.get_lr() 
            wandb_log_train_data(time_cost, lr=lr, core_metric=avg_core_metric, encoder_metric=avg_encoder_metric, images=wb_image)

    def print_checkpoint_info(self, time_cost, epoch_idx, iter_idx, len_dataloader, avg_core_metric = None, avg_encoder_metric = None):
        """Prints formatted information about the current checkpoint."""
        print_iter(epoch_idx, self.parent.num_epoch, iter_idx, len_dataloader, time_cost)
        if avg_core_metric is not None and avg_encoder_metric is not None:
            print_lr(self.encoder_trainer.optimizers, self.core_trainer.optimizers)
        elif avg_core_metric is not None:
            print_lr(self.core_trainer.optimizers)
        elif avg_encoder_metric is not None:
            print_lr(self.encoder_trainer.optimizers)
        print('--------------------Training Metrics--------------------')
        if self.use_encoder and avg_encoder_metric is not None:
            encoder_ccnet_name = self.parent.encoder_ccnet.model_name
            print_trainer(encoder_ccnet_name, avg_encoder_metric)
        if self.use_core and avg_core_metric is not None:
            core_ccnet_name = self.parent.core_ccnet.model_name
            print_trainer(core_ccnet_name, avg_core_metric)

    def reset_metrics(self):
        """resets metrics trackers."""
        self.pivot_time = None
        self.core_metrics.reset()
        self.encoder_metrics.reset()
        self.cnt_checkpoints = 0
        self.cnt_print += 1



