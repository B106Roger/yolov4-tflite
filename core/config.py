#! /usr/bin/env python
# coding=utf-8
from easydict import EasyDict as edict


__C                           = edict()
# Consumers can get config by: from config import cfg

cfg                           = __C

# YOLO options
__C.YOLO                      = edict()

__C.YOLO.CLASSES              = "./data/classes/1cls.names"
__C.YOLO.ANCHORS              = [12,16, 19,36, 40,28, 36,75, 76,55, 72,146, 142,110, 192,243, 459,401]
__C.YOLO.ANCHORS_V3           = [10,13, 16,30, 33,23, 30,61, 62,45, 59,119, 116,90, 156,198, 373,326]
__C.YOLO.ANCHORS_TINY         = [23,27, 37,58, 81,82, 81,82, 135,169, 344,319]
__C.YOLO.STRIDES              = [8, 16, 32]
__C.YOLO.STRIDES_TINY         = [16, 32]
__C.YOLO.XYSCALE              = [1.2, 1.1, 1.05]
__C.YOLO.XYSCALE_TINY         = [1.05, 1.05]
__C.YOLO.ANCHOR_PER_SCALE     = 3
__C.YOLO.IOU_LOSS_THRESH      = 0.5


# Train options
__C.TRAIN                     = edict()

__C.TRAIN.ANNOT_PATHS          = [
    "/mnt/HDD1/Tzuchi/annotation/scale_aware_data_selection_3/train_1cls.txt"
]
__C.TRAIN.BATCH_SIZE          = 8 #2
# __C.TRAIN.INPUT_SIZE        = [320, 352, 384, 416, 448, 480, 512, 544, 576, 608]
__C.TRAIN.INPUT_SIZE          = 608
__C.TRAIN.DATA_AUG            = True
__C.TRAIN.LR_INIT             = 1e-3
__C.TRAIN.LR_END              = 1e-6
__C.TRAIN.WARMUP_EPOCHS       = 2
__C.TRAIN.FISRT_STAGE_EPOCHS    = 20
__C.TRAIN.SECOND_STAGE_EPOCHS   = 30



# TEST options
__C.TEST                      = edict()

#__C.TEST.ANNOT_PATHS           = ["./data/dataset/gis_val_1.txt"]
__C.TEST.ANNOT_PATHS           = [
    "/mnt/HDD1/Tzuchi/annotation/data_selection_3/val_1cls.txt"
]
__C.TEST.BATCH_SIZE           = 2
__C.TEST.INPUT_SIZE           = 608
__C.TEST.DATA_AUG             = False
__C.TEST.DECTECTED_IMAGE_PATH = "./data/detection/"
__C.TEST.SCORE_THRESHOLD      = 0.25
__C.TEST.IOU_THRESHOLD        = 0.5

