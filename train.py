""" 학습 코드

TODO:

NOTES:

REFERENCE:
    * MNC 코드 템플릿 train.py

UPDATED:
"""

import os
import random
from tqdm import tqdm
from datetime import datetime, timezone, timedelta
import numpy as np
import pdb 
import argparse

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import transforms

from modules.metrics import get_metric_fn, FocalLoss
from modules.dataset import CustomDataset , TestDataset, MyLazyDataset
from modules.trainer import Trainer
from modules.utils import load_yaml, save_yaml, get_logger, make_directory
from modules.earlystoppers import LossEarlyStopper
from modules.recorders import PerformanceRecorder
from models.models import *

parser = argparse.ArgumentParser(description="resnet_teacher")
parser.add_argument("--yml", default="train_config", type=str, help='yml file') 
args = parser.parse_args()

os.environ["CUDA_VISIBLE_DEVICES"]="0"

# DEBUG
DEBUG = False

# CONFIG
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_PROJECT_DIR = os.path.dirname(PROJECT_DIR)
DATA_DIR = '../dataset/Tomato'
TRAIN_CONFIG_PATH = os.path.join(PROJECT_DIR, 'config/', args.yml + '.yml')
config = load_yaml(TRAIN_CONFIG_PATH)

# SEED
RANDOM_SEED = config['SEED']['random_seed']

# TRAIN
EPOCHS = config['TRAIN']['num_epochs']
BATCH_SIZE = config['TRAIN']['batch_size']
LEARNING_RATE = float(config['TRAIN']['learning_rate'])
EARLY_STOPPING_PATIENCE = config['TRAIN']['early_stopping_patience']
MODEL = config['TRAIN']['model']
OPTIMIZER = config['TRAIN']['optimizer']
SCHEDULER = config['TRAIN']['scheduler']
MOMENTUM = config['TRAIN']['momentum']
WEIGHT_DECAY = config['TRAIN']['weight_decay']
LOSS_FN = config['TRAIN']['loss_function']
METRIC_FN = config['TRAIN']['metric_function']
INPUT_SHAPE = config['TRAIN']['input_shape']
INPUT_SHAPE = tuple((INPUT_SHAPE,INPUT_SHAPE))
NUM_WORKER = config['DATALOADER']['num_workers']

# TRAIN SERIAL
KST = timezone(timedelta(hours=9))
TRAIN_TIMESTAMP = datetime.now(tz=KST).strftime("%Y%m%d%H%M%S")
TRAIN_SERIAL = f'{MODEL}_{TRAIN_TIMESTAMP}' if DEBUG is not True else 'DEBUG'

# PERFORMANCE RECORD
PERFORMANCE_RECORD_DIR = os.path.join(PROJECT_DIR, 'results', 'train', TRAIN_SERIAL)
PERFORMANCE_RECORD_COLUMN_NAME_LIST = config['PERFORMANCE_RECORD']['column_list']


if __name__ == '__main__':

    # Set random seed
    torch.manual_seed(RANDOM_SEED)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(RANDOM_SEED)
    random.seed(RANDOM_SEED)

    # Set device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Set train result directory
    make_directory(PERFORMANCE_RECORD_DIR)

    # Set system logger
    system_logger = get_logger(name='train', file_path=os.path.join(PERFORMANCE_RECORD_DIR, 'train_log.log'))
    
    # Load dataset & dataloader
    train_dataset = CustomDataset(data_dir=DATA_DIR, mode='train', input_shape=INPUT_SHAPE)
    ratio = 0.8
    lengths = [int(len(train_dataset)*ratio), len(train_dataset)-int(len(train_dataset)*(ratio))]
    train_set, val_set = torch.utils.data.random_split(train_dataset, lengths)   # random split the dataset into trainset & valset
    train_data = MyLazyDataset(train_set, input_shape=INPUT_SHAPE, mode="train")
    val_data = MyLazyDataset(val_set, input_shape=INPUT_SHAPE, mode="val")

    train_dataloader = DataLoader(train_data, batch_size=BATCH_SIZE, shuffle=True,
        num_workers=NUM_WORKER,
        pin_memory=True,)
    validation_dataloader = DataLoader(val_data, batch_size=BATCH_SIZE, shuffle=True,
        num_workers=NUM_WORKER,
        pin_memory=True,)

    # check lenghts of data loader (train, val)
    print('Train set samples before split:',len(train_dataset))    
    print('Train set samples:',len(train_data))    
    print('Validation set samples:',len(val_data))

    
    # Load Model
    model = get_my_model(model_name=MODEL, num_classes = 10)
    model.to(device)

    # # # # # # # Set optimizer, scheduler, loss function, metric function
    ## Optimizer
    if OPTIMIZER == "sgd":
        optimizer = optim.SGD(model.parameters(), lr=LEARNING_RATE, momentum=0.9, weight_decay=WEIGHT_DECAY)
    if OPTIMIZER == "adam":
        optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)

    ## Scheduler
    if SCHEDULER == "msl":  # => mainly used
        hp_lr_decay_ratio = 0.2
        scheduler = optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=[
                EPOCHS * 0.2,
                EPOCHS * 0.4,
                EPOCHS * 0.6,
                EPOCHS * 0.8,
                # EPOCHS * 0.3,
                # EPOCHS * 0.6,
                # EPOCHS * 0.8,
            ],
            gamma=hp_lr_decay_ratio,
        )
    if SCHEDULER == "cos":
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)
    if SCHEDULER == "plateu": 
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", patience=5, factor=0.5)

    ## Loss function
    if LOSS_FN == "ce":
        # Cross Entropy Loss
        criterion = nn.CrossEntropyLoss()
    if LOSS_FN == "w_ce":          
        # Weight Cross Entropy Loss
        # Use the calculated num of img in trainset for cross entropy weights.
        _, num_imgs_class  = train_dataset.data_loader()
        num_imgs_class = torch.FloatTensor(num_imgs_class)
        print("num of imgs for classes:", num_imgs_class)
        class_percentage = num_imgs_class / num_imgs_class.sum()
        class_weights = 1.0 /class_percentage
        class_weights = (class_weights / class_weights.sum())
        class_weights = torch.exp(class_weights)
        class_weights = class_weights.to(device)
        print("weights for classes:", class_weights)
        criterion = nn.CrossEntropyLoss(weight=class_weights)
    if LOSS_FN == "focal_loss":
        # Focal Loss for imbalanced class
        criterion = FocalLoss(alpha=1, gamma=2, reduce=True)
    if LOSS_FN == "w_focal_loss":
        # Combine Focal Loss & Weight Cross Entropy Loss
        _, num_imgs_class  = train_dataset.data_loader()
        num_imgs_class = torch.FloatTensor(num_imgs_class)
        print("num of imgs for classes:", num_imgs_class)
        class_percentage = num_imgs_class / num_imgs_class.sum()
        class_weights = 1.0 /class_percentage
        class_weights = (class_weights / class_weights.sum())
        class_weights = torch.exp(class_weights)
        class_weights = class_weights.to(device)
        print("weights for classes:", class_weights)
        criterion = FocalLoss(alpha=1, gamma=2, weight=class_weights, reduce=True)

    ## Metric Function
    metric_fn = get_metric_fn

    
    # # # # # # # Set trainer, Earlystopper
    # Set trainer
    trainer = Trainer(criterion, model, device, metric_fn, optimizer, scheduler, logger=system_logger)

    # Set earlystopper
    early_stopper = LossEarlyStopper(patience=EARLY_STOPPING_PATIENCE, verbose=True, logger=system_logger)

    # Set performance recorder
    key_column_value_list = [
        TRAIN_SERIAL,
        TRAIN_TIMESTAMP,
        MODEL,
        OPTIMIZER,
        LOSS_FN,
        METRIC_FN,
        EARLY_STOPPING_PATIENCE,
        BATCH_SIZE,
        EPOCHS,
        LEARNING_RATE,
        WEIGHT_DECAY,
        RANDOM_SEED]

    performance_recorder = PerformanceRecorder(column_name_list=PERFORMANCE_RECORD_COLUMN_NAME_LIST,
                                               record_dir=PERFORMANCE_RECORD_DIR,
                                               key_column_value_list=key_column_value_list,
                                               logger=system_logger,
                                               model=model,
                                               optimizer=optimizer,
                                               scheduler=scheduler)

    # Train
    save_yaml(os.path.join(PERFORMANCE_RECORD_DIR, 'train_config.yaml'), config)
    criterion = 1E+8
    for epoch_index in range(EPOCHS):

        trainer.train_epoch(train_dataloader, epoch_index)
        trainer.validate_epoch(validation_dataloader, epoch_index, 'val')
        scheduler.step()
        # scheduler.step(trainer.train_mean_loss)  # for plateu scheduler
        
        for param_group in optimizer.param_groups:  # to see the learning rate per epoch
            current_lr =  param_group['lr']
        print("current_lr", current_lr)
        print("current_lr", optimizer.param_groups[0]['lr'])

        # Performance record - csv & save elapsed_time
        performance_recorder.add_row(epoch_index=epoch_index,
                                     train_loss=trainer.train_mean_loss,
                                     validation_loss=trainer.val_mean_loss,
                                     train_score=trainer.train_score,
                                     validation_score=trainer.validation_score)
        
        # Performance record - plot
        performance_recorder.save_performance_plot(final_epoch=epoch_index)

        # early_stopping check
        early_stopper.check_early_stopping(loss=trainer.val_mean_loss)

        if early_stopper.stop:
            print('Early stopped')
            break

        if trainer.val_mean_loss < criterion:
            criterion = trainer.val_mean_loss
            performance_recorder.weight_path = os.path.join(PERFORMANCE_RECORD_DIR, 'best.pt')
            performance_recorder.save_weight()
            print(f'{epoch_index} model saved')
            print('----------------------------------')


        

