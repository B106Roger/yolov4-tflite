import sys, os
from core.common import MishLayer,BatchNormalization
from absl import app, flags, logging
from absl.flags import FLAGS
from core.accumulator import Accumulator
import os, shutil
import tensorflow as tf
from core.yolov4 import YOLO, compute_loss, compute_da_loss, decode_train
from core.dataset_tiny import Dataset, tfDataset, tfAdversailDataset
from core.config_adverserial import cfg
import numpy as np
from core import utils
from core.utils import freeze_all, unfreeze_all
from tqdm import tqdm
import tensorflow_model_optimization as tfmot
import time

flags.DEFINE_string('model', 'yolov4', 'yolov4, yolov3')
flags.DEFINE_string('weights', None, 'pretrained weights')
flags.DEFINE_boolean('tiny', True, 'yolo or yolo-tiny')
flags.DEFINE_boolean('qat', False, 'train w/ or w/o quatize aware')
flags.DEFINE_string('save_dir', 'checkpoints/yolov4_tiny', 'save model dir')
tf.config.optimizer.set_jit(True)

def apply_quantization(layer):
    if isinstance(layer, tf.python.keras.engine.base_layer.TensorFlowOpLayer):
         return layer
    return tfmot.quantization.keras.quantize_annotate_layer(layer)

def qa_train(model):
    # qa_train part
    quantize_model = tfmot.quantization.keras.quantize_model
    quantize_scope = tfmot.quantization.keras.quantize_scope
    
    annotated_model = tf.keras.models.clone_model(model,
        clone_function=apply_quantization,
    )

    quant_aware_model = tfmot.quantization.keras.quantize_apply(annotated_model)
    return quant_aware_model

def copytree(src, dst, symlinks=False, ignore=None):
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, symlinks, ignore)
        else:
            shutil.copy2(s, d)

def main(_argv):
    physical_devices = tf.config.experimental.list_physical_devices('GPU')
    os.makedirs(FLAGS.save_dir, exist_ok=True)
    os.makedirs(os.path.join(FLAGS.save_dir, 'config'), exist_ok=True)
    os.makedirs(os.path.join(FLAGS.save_dir, 'ckpt'), exist_ok=True)

    testset_source = tfDataset(FLAGS, is_training=False, filter_area=123, use_imgaug=False).dataset_gen()
    testset_target = tfAdversailDataset(FLAGS, is_training=False, filter_area=123, use_imgaug=False).dataset_gen()
    testset = tf.data.Dataset.zip((testset_source, testset_target))

    trainset_source = tfDataset(FLAGS, is_training=True, filter_area=123, use_imgaug=False).dataset_gen()
    trainset_target = tfAdversailDataset(FLAGS, is_training=True, filter_area=123, use_imgaug=False).dataset_gen()
    trainset = tf.data.Dataset.zip((trainset_source, trainset_target))

    copytree('./core', os.path.join(FLAGS.save_dir, 'config'))
    shutil.copy2(sys.argv[0], os.path.join(FLAGS.save_dir, 'config', os.path.basename(sys.argv[0])))
    with open(os.path.join(FLAGS.save_dir, 'command.txt'), 'w') as f:
        f.writelines(' '.join(sys.argv))
    f.close()


    logdir = os.path.join(FLAGS.save_dir, 'logs')
    isfreeze = False
    steps_per_epoch = len(trainset)
    first_stage_epochs = cfg.TRAIN.FISRT_STAGE_EPOCHS
    second_stage_epochs = cfg.TRAIN.SECOND_STAGE_EPOCHS
    global_steps = tf.Variable(1, trainable=False, dtype=tf.int64)
    warmup_steps = cfg.TRAIN.WARMUP_EPOCHS * steps_per_epoch
    total_steps = (first_stage_epochs + second_stage_epochs) * steps_per_epoch

    input_layer = tf.keras.layers.Input([cfg.TRAIN.INPUT_SIZE, cfg.TRAIN.INPUT_SIZE, 3])
    STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)
    IOU_LOSS_THRESH = cfg.YOLO.IOU_LOSS_THRESH

    freeze_layers = utils.load_freeze_layer(FLAGS.model, FLAGS.tiny)

    # [conv_mbox(38,38), conv_lbbox(19,19)]
    output_maps = YOLO(input_layer, NUM_CLASS, FLAGS.model, FLAGS.tiny, use_dc_head=True)
    feature_maps = output_maps[:2]
    da_maps = output_maps[2:]

    # Decoding YOLOv4 Output
    if FLAGS.tiny:
        bbox_tensors = []
        for i, fm in enumerate(feature_maps):
            if i == 0:
                bbox_tensor = decode_train(fm, cfg.TRAIN.INPUT_SIZE // 16, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE)
            else:
                bbox_tensor = decode_train(fm, cfg.TRAIN.INPUT_SIZE // 32, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE)
            bbox_tensors.append(fm)
            bbox_tensors.append(bbox_tensor)
    else:
        bbox_tensors = []
        for i, fm in enumerate(feature_maps):
            if i == 0:
                bbox_tensor = decode_train(fm, cfg.TRAIN.INPUT_SIZE // 8, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE)
            elif i == 1:
                bbox_tensor = decode_train(fm, cfg.TRAIN.INPUT_SIZE // 16, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE)
            else:
                bbox_tensor = decode_train(fm, cfg.TRAIN.INPUT_SIZE // 32, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE)
            bbox_tensors.append(fm)
            bbox_tensors.append(bbox_tensor)

    model = tf.keras.Model(input_layer, {
        'raw_bbox_m': bbox_tensors[0],          # tensor size of feature map
        'bbox_m': bbox_tensors[1],
        'da_m': da_maps[0],
        'raw_bbox_l': bbox_tensors[2],
        'bbox_l': bbox_tensors[3],
        'da_l': da_maps[1],
    })
    if FLAGS.weights == None:
        print("Training from scratch ......................")
    else:
        if FLAGS.weights.split(".")[len(FLAGS.weights.split(".")) - 1] == "weights":
            utils.load_weights(model, FLAGS.weights, FLAGS.model, FLAGS.tiny)
        else:
            model.load_weights(FLAGS.weights)
        print('Restoring weights from: %s ... ' % FLAGS.weights)
    
    if (FLAGS.qat):
        model = qa_train(model)
        print("Training in Quatization Aware ................. ")
    
    model.summary()

    optimizer = tf.keras.optimizers.Adam()
    if os.path.exists(logdir): shutil.rmtree(logdir)
    writer = tf.summary.create_file_writer(logdir)

    @tf.function
    def train_step(source_image_data, source_train_target, target_image_data):
        with tf.GradientTape() as tape:
            source_dict_result = model(source_image_data, training=True)
            target_dict_result = model(target_image_data, training=True)

            pred_result = [
                source_dict_result['raw_bbox_m'], 
                source_dict_result['bbox_m'], 
                source_dict_result['raw_bbox_l'],
                source_dict_result['bbox_l'],
            ]
            source_da_reuslt = [
                source_dict_result['da_m'],
                source_dict_result['da_l'],
            ]
            target_da_result = [
                target_dict_result['da_m'],
                target_dict_result['da_l']
            ]

            giou_loss = conf_loss = prob_loss = da_loss = 0
            # optimizing process
            for i in range(len(freeze_layers)):
                conv, pred = pred_result[i * 2], pred_result[i * 2 + 1]
                loss_items = compute_loss(pred, conv, source_train_target[i][0], source_train_target[i][1], STRIDES=STRIDES, NUM_CLASS=NUM_CLASS, IOU_LOSS_THRESH=IOU_LOSS_THRESH, i=i)
                giou_loss += loss_items[0]
                conf_loss += loss_items[1]
                prob_loss += loss_items[2]

            da_loss_items = compute_da_loss(source_da_reuslt, target_da_result)
            da_loss   +=  da_loss_items['da_source_loss'] + da_loss_items['da_target_loss']


            total_loss = giou_loss + conf_loss + prob_loss + da_loss

            gradients = tape.gradient(total_loss, model.trainable_variables)
            optimizer.apply_gradients(zip(gradients, model.trainable_variables))

        # writing summary data
        with writer.as_default():
            tf.summary.scalar("lr", optimizer.lr, step=global_steps)
            tf.summary.scalar("loss/total_loss", total_loss, step=global_steps)
            tf.summary.scalar("loss/giou_loss", giou_loss, step=global_steps)
            tf.summary.scalar("loss/conf_loss", conf_loss, step=global_steps)
            tf.summary.scalar("loss/prob_loss", prob_loss, step=global_steps)
            tf.summary.scalar("loss/da_loss", da_loss, step=global_steps)
            tf.summary.scalar("loss/da_source_loss", da_loss_items['da_source_loss'], step=global_steps)
            tf.summary.scalar("loss/da_target_loss", da_loss_items['da_target_loss'], step=global_steps)

        writer.flush()
        return {
            'giou_loss': giou_loss, 
            'conf_loss': conf_loss, 
            'prob_loss': prob_loss,
            'da_loss':   da_loss,
        }

    @tf.function
    def test_step(source_image_data, source_train_target, target_image_data):
        source_dict_result = model(source_image_data, training=False)
        target_dict_result = model(target_image_data, training=False)

        pred_result = [
            source_dict_result['raw_bbox_m'], 
            source_dict_result['bbox_m'], 
            source_dict_result['raw_bbox_l'],
            source_dict_result['bbox_l'],
        ]
        source_da_reuslt = [
            source_dict_result['da_m'],
            source_dict_result['da_l'],
        ]
        target_da_result = [
            target_dict_result['da_m'],
            target_dict_result['da_l']
        ]
        giou_loss = conf_loss = prob_loss = da_loss = 0
        # optimizing process
        for i in range(len(freeze_layers)):
            conv, pred = pred_result[i * 2], pred_result[i * 2 + 1]
            loss_items = compute_loss(pred, conv, source_train_target[i][0], source_train_target[i][1], STRIDES=STRIDES, NUM_CLASS=NUM_CLASS, IOU_LOSS_THRESH=IOU_LOSS_THRESH, i=i)
            giou_loss += loss_items[0]
            conf_loss += loss_items[1]
            prob_loss += loss_items[2]

        da_loss_items = compute_da_loss(source_da_reuslt, target_da_result)
        da_loss   +=  da_loss_items['da_source_loss'] + da_loss_items['da_target_loss']

        total_loss = giou_loss + conf_loss + prob_loss + da_loss
        return {
            'giou_loss': giou_loss, 
            'conf_loss': conf_loss, 
            'prob_loss': prob_loss,
            'da_loss': da_loss,
            'total_loss': total_loss
        }


    for epoch in range(1, 1+first_stage_epochs + second_stage_epochs):
        if epoch <= first_stage_epochs:
            if not isfreeze:
                isfreeze = True
                for name in freeze_layers:
                    if FLAGS.qat==True:
                        freeze = model.get_layer('quant_'+name)
                    else:
                        freeze = model.get_layer(name)
                    freeze_all(freeze)
        elif epoch > first_stage_epochs:
            if isfreeze:
                isfreeze = False
                for name in freeze_layers:
                    if FLAGS.qat==True:
                        freeze = model.get_layer('quant_'+name)
                    else:
                        freeze = model.get_layer(name)
                    unfreeze_all(freeze)
        
        giou_loss_counter = Accumulator()
        conf_loss_counter = Accumulator()
        prob_loss_counter = Accumulator()
        da_loss_counter = Accumulator()

        tmp=time.time()
        with tqdm(total=len(trainset), ncols=200) as pbar:
            for data_item in trainset:
                source_data_dict=data_item[0]
                target_data_dict=data_item[1]

                source_images=source_data_dict['images']
                source_train_targets=[
                    [source_data_dict['label_bboxes_m'], source_data_dict['bboxes_m']], 
                    [source_data_dict['label_bboxes_l'], source_data_dict['bboxes_l']], 
                ]
                target_images=target_data_dict['images']

                data_time=time.time()-tmp
                batch_size = source_images.shape[0]
                tmp=time.time()
                loss_dict = train_step(source_images, source_train_targets, target_images)
                model_time = time.time()-tmp

                giou_loss_counter.update(loss_dict['giou_loss'], batch_size)
                conf_loss_counter.update(loss_dict['conf_loss'], batch_size)
                prob_loss_counter.update(loss_dict['prob_loss'], batch_size)
                da_loss_counter.update(loss_dict['da_loss'], batch_size)

                # update learning rate
                global_steps.assign_add(1)
                if global_steps < warmup_steps:
                    lr = global_steps / warmup_steps * cfg.TRAIN.LR_INIT
                else:
                    lr = cfg.TRAIN.LR_END + 0.5 * (cfg.TRAIN.LR_INIT - cfg.TRAIN.LR_END) * (
                        (1 + tf.cos((global_steps - warmup_steps) / (total_steps - warmup_steps) * np.pi)))
                optimizer.lr.assign(lr.numpy())

                total = giou_loss_counter.get_average() \
                    + conf_loss_counter.get_average() \
                    + prob_loss_counter.get_average() \
                    + da_loss_counter.get_average()

                pbar.set_postfix({
                    'lr': f"{lr.numpy():6.4f}",
                    'giou_loss': f"{giou_loss_counter.get_average():6.4f}",
                    'conf_loss': f"{conf_loss_counter.get_average():6.4f}",
                    'prob_loss': f"{prob_loss_counter.get_average():6.4f}",
                    'da_loss': f"{da_loss_counter.get_average():6.4f}",
                    'total': f"{total: 6.4f}",
                    'data_time': f'{data_time:6.4f}',
                    'model_time': f'{model_time:6.4f}'
                })
                total_epoch = first_stage_epochs + second_stage_epochs
                pbar.set_description(f"Epoch {epoch:3d}/{total_epoch:3d}")
                pbar.update(1)

                tmp=time.time()

        if epoch % 5 == 0:
            giou_loss_counter.reset()
            conf_loss_counter.reset()
            prob_loss_counter.reset()
            da_loss_counter.reset()
            with tqdm(total=len(testset), ncols=150, desc=f"{'Test':<13}") as pbar:
                batch_size = source_images.shape[0]
                for data_item in testset:
                    source_data_dict=data_item[0]
                    target_data_dict=data_item[1]

                    source_images=source_data_dict['images']
                    source_train_targets=[
                        [source_data_dict['label_bboxes_m'], source_data_dict['bboxes_m']], 
                        [source_data_dict['label_bboxes_l'], source_data_dict['bboxes_l']], 
                    ]
                    target_images=target_data_dict['images']

                    batch_size = source_images.shape[0]
                    loss_dict = test_step(source_images, source_train_targets, target_images)

                    giou_loss_counter.update(loss_dict['giou_loss'], batch_size)
                    conf_loss_counter.update(loss_dict['conf_loss'], batch_size)
                    prob_loss_counter.update(loss_dict['prob_loss'], batch_size)
                    da_loss_counter.update(loss_dict['da_loss'], batch_size)
                    total = giou_loss_counter.get_average() \
                        + conf_loss_counter.get_average() \
                        + prob_loss_counter.get_average() \
                        + da_loss_counter.get_average()

                    pbar.set_postfix({
                        'giou_loss': f"{giou_loss_counter.get_average():6.4f}",
                        'conf_loss': f"{conf_loss_counter.get_average():6.4f}",
                        'prob_loss': f"{prob_loss_counter.get_average():6.4f}",
                        'da_loss': f"{da_loss_counter.get_average():6.4f}",
                        'total': f"{total: 6.4f}"
                    })
                    pbar.update(1)
            
            # writing summary data
            with writer.as_default():
                tf.summary.scalar("val/total_loss", total, step=epoch)
                tf.summary.scalar("val/giou_loss", giou_loss_counter.get_average(), step=epoch)
                tf.summary.scalar("val/conf_loss", conf_loss_counter.get_average(), step=epoch)
                tf.summary.scalar("val/prob_loss", prob_loss_counter.get_average(), step=epoch)
                tf.summary.scalar("val/da_loss", da_loss_counter.get_average(), step=epoch)
            
            model.save_weights(os.path.join(FLAGS.save_dir, 'ckpt', f'{epoch:04d}.ckpt'))
        
    model.save_weights(os.path.join(FLAGS.save_dir, 'ckpt', 'final.ckpt'))

if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass
