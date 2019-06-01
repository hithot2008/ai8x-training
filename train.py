#!/usr/bin/env python3
###################################################################################################
#
# Copyright (C) 2018-2019 Maxim Integrated Products, Inc. All Rights Reserved.
#
# Maxim Confidential
#
###################################################################################################
#
# Portions Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""This is an example application for compressing image classification models.

The application borrows its main flow code from torchvision's ImageNet classification
training sample application (https://github.com/pytorch/examples/tree/master/imagenet).
We tried to keep it similar, in order to make it familiar and easy to understand.

Integrating compression is very simple: simply add invocations of the appropriate
compression_scheduler callbacks, for each stage in the training.  The training skeleton
looks like the pseudo code below.  The boiler-plate Pytorch classification training
is speckled with invocations of CompressionScheduler.

For each epoch:
    compression_scheduler.on_epoch_begin(epoch)
    train()
    validate()
    save_checkpoint()
    compression_scheduler.on_epoch_end(epoch)

train():
    For each training step:
        compression_scheduler.on_minibatch_begin(epoch)
        output = model(input)
        loss = criterion(output, target)
        compression_scheduler.before_backward_pass(epoch)
        loss.backward()
        compression_scheduler.before_parameter_optimization(epoch)
        optimizer.step()
        compression_scheduler.on_minibatch_end(epoch)


This exmple application can be used with torchvision's ImageNet image classification
models, or with the provided sample models:

- ResNet for CIFAR: https://github.com/junyuseu/pytorch-cifar-models
- MobileNet for ImageNet: https://github.com/marvis/pytorch-mobilenet
"""

import math
import time
import os
import traceback
import logging
from collections import OrderedDict
from functools import partial
from pydoc import locate
import operator
import parser
import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchnet.meter as tnt
import torchvision
from torchvision import transforms
import distiller
import distiller.apputils as apputils
import distiller.model_summaries as model_summaries
from distiller.data_loggers.collector import SummaryActivationStatsCollector, \
    RecordsActivationStatsCollector, QuantCalibrationStatsCollector, \
    collectors_context
from distiller.quantization.range_linear import PostTrainLinearQuantizer
from distiller.data_loggers.logger import TensorBoardLogger, PythonLogger
import examples.automated_deep_compression as adc
from range_linear_ai84 import PostTrainLinearQuantizerAI84


# Logger handle
msglogger = None


def main():
    script_dir = os.path.dirname(__file__)
    module_path = os.path.abspath(os.path.join(script_dir, '..', '..'))
    global msglogger  # pylint: disable=global-statement

    supported_models = [
        {'name': 'ai84net5',
         'module': 'ai84net',
         'min_input': 1,
         'dim': 2}
    ]

    supported_sources = [
        {'name': 'MNIST',
         'input': (1, 28, 28),
         'output': list(map(str, range(10))),
         'loader': mnist_get_datasets},
        {'name': 'FashionMNIST',
         'input': (1, 28, 28),
         'output': ('top', 'trouser', 'pullover', 'dress', 'coat', 'sandal',
                    'shirt', 'sneaker', 'bag', 'boot'),
         'loader': fashionmnist_get_datasets},
        {'name': 'CIFAR10',
         'input': (3, 32, 32),
         'output': ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse',
                    'ship', 'truck'),
         'loader': cifar10_get_datasets}
    ]

    model_names = [item['name'] for item in supported_models]
    dataset_names = [item['name'] for item in supported_sources]
    # Parse arguments
    args = parser.get_parser(model_names, dataset_names).parse_args()
    if args.epochs is None:
        args.epochs = 90

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    msglogger = apputils.config_pylogger(os.path.join(script_dir, 'logging.conf'), args.name,
                                         args.output_dir)

    # Log various details about the execution environment.  It is sometimes useful
    # to refer to past experiment executions and this information may be useful.
    apputils.log_execution_env_state(args.compress, msglogger.logdir, gitroot=module_path)
    msglogger.debug("Distiller: %s", distiller.__version__)

    start_epoch = 0
    ending_epoch = args.epochs
    perf_scores_history = []

    if args.evaluate:
        args.deterministic = True
    if args.deterministic:
        distiller.set_deterministic(args.seed)  # For experiment reproducability
        if args.seed is not None:
            distiller.set_seed(args.seed)
    else:
        # Turn on CUDNN benchmark mode for best performance. This is usually "safe" for image
        # classification models, as the input sizes don't change during the run
        # See here:
        # https://discuss.pytorch.org/t/what-does-torch-backends-cudnn-benchmark-do/5936/3
        cudnn.benchmark = True

    if args.cpu or not torch.cuda.is_available():
        # Set GPU index to -1 if using CPU
        args.device = 'cpu'
        args.gpus = -1
    else:
        args.device = 'cuda'
        if args.gpus is not None:
            try:
                args.gpus = [int(s) for s in args.gpus.split(',')]
            except ValueError:
                raise ValueError('ERROR: Argument --gpus must be a comma-separated '
                                 'list of integers only')
            available_gpus = torch.cuda.device_count()
            for dev_id in args.gpus:
                if dev_id >= available_gpus:
                    raise ValueError('ERROR: GPU device ID {0} requested, but only {1} '
                                     'devices available'
                                     .format(dev_id, available_gpus))
            # Set default device in case the first one on the list != 0
            torch.cuda.set_device(args.gpus[0])

    if args.earlyexit_thresholds:
        args.num_exits = len(args.earlyexit_thresholds) + 1
        args.loss_exits = [0] * args.num_exits
        args.losses_exits = []
        args.exiterrors = []

    selected_source = next((item for item in supported_sources if item['name'] == args.dataset))
    labels = selected_source['output']
    args.num_classes = len(labels)
    dimensions = selected_source['input']
    args.datasets_fn = selected_source['loader']

    # Create the model
    module = next(item for item in supported_models if item['name'] == args.cnn)
    Model = locate(module['module'] + '.' + args.cnn)
    if not Model:
        raise RuntimeError("Model " + args.cnn + " not found\n")
    if module['dim'] > 1 and module['min_input'] > dimensions[2]:
        model = Model(pretrained=False, num_classes=args.num_classes,
                      num_channels=dimensions[0],
                      dimensions=(dimensions[1], dimensions[2]),
                      padding=(module['min_input'] - dimensions[2] + 1) // 2,
                      simulate=args.act_mode_8bit).to(args.device)
    else:
        model = Model(pretrained=False, num_classes=args.num_classes,
                      num_channels=dimensions[0],
                      dimensions=(dimensions[1], dimensions[2]),
                      simulate=args.act_mode_8bit).to(args.device)
    # if args.add_logsoftmax:
    #     model = nn.Sequential(model, nn.LogSoftmax(dim=1))
    # if args.add_softmax:
    #     model = nn.Sequential(model, nn.Softmax(dim=1))

    compression_scheduler = None
    # Create a couple of logging backends.  TensorBoardLogger writes log files in a format
    # that can be read by Google's Tensor Board.  PythonLogger writes to the Python logger.
    tflogger = TensorBoardLogger(msglogger.logdir)
    pylogger = PythonLogger(msglogger)

    # capture thresholds for early-exit training
    if args.earlyexit_thresholds:
        msglogger.info('=> using early-exit threshold values of %s', args.earlyexit_thresholds)

    # We can optionally resume from a checkpoint
    optimizer = None
    if args.resumed_checkpoint_path:
        model, compression_scheduler, optimizer, start_epoch = apputils.load_checkpoint(
            model, args.resumed_checkpoint_path, model_device=args.device)
    elif args.load_model_path:
        model = apputils.load_lean_checkpoint(model, args.load_model_path,
                                              model_device=args.device)
    if args.reset_optimizer:
        start_epoch = 0
        if optimizer is not None:
            optimizer = None
            msglogger.info('\nreset_optimizer flag set: Overriding resumed optimizer and '
                           'resetting epoch count to 0')

    # Define loss function (criterion)
    criterion = nn.CrossEntropyLoss().to(args.device)

    if optimizer is None:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum,
                                    weight_decay=args.weight_decay)
        msglogger.info('Optimizer Type: %s', type(optimizer))
        msglogger.info('Optimizer Args: %s', optimizer.defaults)

    if args.AMC:
        return automated_deep_compression(model, criterion, optimizer, pylogger, args)
    if args.greedy:
        return greedy(model, criterion, optimizer, pylogger, args)

    # This sample application can be invoked to produce various summary reports.
    if args.summary:
        return summarize_model(model, args.dataset, which_summary=args.summary)

    activations_collectors = create_activation_stats_collectors(model, *args.activation_stats)

    if args.qe_calibration:
        msglogger.info('Quantization calibration stats collection enabled:')
        msglogger.info('\tStats will be collected for {:.1%} '
                       'of test dataset'.format(args.qe_calibration))
        msglogger.info('\tSetting constant seeds and converting model to serialized execution')
        distiller.set_deterministic()
        model = distiller.make_non_parallel_copy(model)
        activations_collectors.update(create_quantization_stats_collector(model))
        args.evaluate = True
        args.effective_test_size = args.qe_calibration

    # Load the datasets
    train_loader, val_loader, test_loader, _ = apputils.get_data_loaders(
        args.datasets_fn, (os.path.expanduser(args.data), args), args.batch_size,
        args.workers, args.validation_split, args.deterministic,
        args.effective_train_size, args.effective_valid_size, args.effective_test_size)
    msglogger.info('Dataset sizes:\n\ttraining=%d\n\tvalidation=%d\n\ttest=%d',
                   len(train_loader.sampler), len(val_loader.sampler), len(test_loader.sampler))

    if args.sensitivity is not None:
        sensitivities = np.arange(args.sensitivity_range[0], args.sensitivity_range[1],
                                  args.sensitivity_range[2])
        return sensitivity_analysis(model, criterion, test_loader, pylogger, args, sensitivities)

    if args.evaluate:
        return evaluate_model(model, criterion, test_loader, pylogger, activations_collectors,
                              args, compression_scheduler)

    if args.compress:
        # The main use-case for this sample application is CNN compression. Compression
        # requires a compression schedule configuration file in YAML.
        compression_scheduler = distiller.file_config(model, optimizer, args.compress,
                                                      compression_scheduler,
                                                      (start_epoch-1)
                                                      if args.resumed_checkpoint_path else None)
    elif compression_scheduler is None:
        compression_scheduler = distiller.CompressionScheduler(model)

    # Model is re-transferred to GPU in case parameters were added (e.g. PACTQuantizer)
    model.to(args.device)

    if args.thinnify:
        # zeros_mask_dict = distiller.create_model_masks_dict(model)
        assert args.resumed_checkpoint_path is not None, \
            "You must use --resume-from to provide a checkpoint file to thinnify"
        distiller.remove_filters(model, compression_scheduler.zeros_mask_dict, args.cnn,
                                 args.dataset, optimizer=None)
        apputils.save_checkpoint(0, args.cnn, model, optimizer=None,
                                 scheduler=compression_scheduler,
                                 name="{}_thinned".format(args.resumed_checkpoint_path.
                                                          replace(".pth.tar", "")),
                                 dir=msglogger.logdir)
        print("Note: your model may have collapsed to random inference, "
              "so you may want to fine-tune")
        return

    args.kd_policy = None
    if args.kd_teacher:
        teacher = Model(pretrained=False, num_classes=args.num_classes, num_channels=dimensions[0])
        if args.kd_resume:
            teacher = apputils.load_lean_checkpoint(teacher, args.kd_resume)
        dlw = distiller.DistillationLossWeights(args.kd_distill_wt, args.kd_student_wt,
                                                args.kd_teacher_wt)
        args.kd_policy = distiller.KnowledgeDistillationPolicy(model, teacher, args.kd_temp, dlw)
        compression_scheduler.add_policy(args.kd_policy, starting_epoch=args.kd_start_epoch,
                                         ending_epoch=args.epochs, frequency=1)

        msglogger.info('\nStudent-Teacher knowledge distillation enabled:')
        msglogger.info('\tTeacher Model: %s', args.kd_teacher)
        msglogger.info('\tTemperature: %s', args.kd_temp)
        msglogger.info('\tLoss Weights (distillation | student | teacher): %s',
                       ' | '.join(['{:.2f}'.format(val) for val in dlw]))
        msglogger.info('\tStarting from Epoch: %s', args.kd_start_epoch)

    if start_epoch >= ending_epoch:
        msglogger.error('epoch count is too low, starting epoch is {} but total epochs set '
                        'to {}'.format(start_epoch, ending_epoch))
        raise ValueError('Epochs parameter is too low. Nothing to do.')

    vloss = 10**6
    for epoch in range(start_epoch, ending_epoch):
        # This is the main training loop.
        msglogger.info('\n')
        if compression_scheduler:
            compression_scheduler.on_epoch_begin(epoch, metrics=vloss)

        # Train for one epoch
        with collectors_context(activations_collectors["train"]) as collectors:
            train(train_loader, model, criterion, optimizer, epoch, compression_scheduler,
                  loggers=[tflogger, pylogger], args=args)
            distiller.log_weights_sparsity(model, epoch, loggers=[tflogger, pylogger])
            distiller.log_activation_statsitics(epoch, "train", loggers=[tflogger],
                                                collector=collectors["sparsity"])
            if args.masks_sparsity:
                msglogger.info(distiller.masks_sparsity_tbl_summary(model, compression_scheduler))

        # evaluate on validation set
        with collectors_context(activations_collectors["valid"]) as collectors:
            top1, top5, vloss = validate(val_loader, model, criterion, [pylogger], args, epoch)
            distiller.log_activation_statsitics(epoch, "valid", loggers=[tflogger],
                                                collector=collectors["sparsity"])
            save_collectors_data(collectors, msglogger.logdir)

        stats = ('Performance/Validation/',
                 OrderedDict([('Loss', vloss),
                              ('Top1', top1),
                              ('Top5', top5)]))
        distiller.log_training_progress(stats, None, epoch, steps_completed=0, total_steps=1,
                                        log_freq=1, loggers=[tflogger])

        if compression_scheduler:
            compression_scheduler.on_epoch_end(epoch, optimizer)

        # Update the list of top scores achieved so far, and save the checkpoint
        update_training_scores_history(perf_scores_history, model, top1, top5, epoch,
                                       args.num_best_scores)
        is_best = epoch == perf_scores_history[0].epoch
        checkpoint_extras = {'current_top1': top1,
                             'best_top1': perf_scores_history[0].top1,
                             'best_epoch': perf_scores_history[0].epoch}
        apputils.save_checkpoint(epoch, args.cnn, model, optimizer=optimizer,
                                 scheduler=compression_scheduler, extras=checkpoint_extras,
                                 is_best=is_best, name=args.name, dir=msglogger.logdir)

    # Finally run results on the test set
    test(test_loader, model, criterion, [pylogger], activations_collectors, args=args)


OVERALL_LOSS_KEY = 'Overall Loss'
OBJECTIVE_LOSS_KEY = 'Objective Loss'


def train(train_loader, model, criterion, optimizer, epoch,
          compression_scheduler, loggers, args):
    """Training loop for one epoch."""
    losses = OrderedDict([(OVERALL_LOSS_KEY, tnt.AverageValueMeter()),
                          (OBJECTIVE_LOSS_KEY, tnt.AverageValueMeter())])

    classerr = tnt.ClassErrorMeter(accuracy=True, topk=(1, 5))
    batch_time = tnt.AverageValueMeter()
    data_time = tnt.AverageValueMeter()

    # For Early Exit, we define statistics for each exit
    # So exiterrors is analogous to classerr for the non-Early Exit case
    if args.earlyexit_lossweights:
        args.exiterrors = []
        for exitnum in range(args.num_exits):
            args.exiterrors.append(tnt.ClassErrorMeter(accuracy=True, topk=(1, 5)))

    total_samples = len(train_loader.sampler)
    batch_size = train_loader.batch_size
    steps_per_epoch = math.ceil(total_samples / batch_size)
    msglogger.info('Training epoch: %d samples (%d per mini-batch)', total_samples, batch_size)

    # Switch to train mode
    model.train()
    acc_stats = []
    end = time.time()
    for train_step, (inputs, target) in enumerate(train_loader):
        # Measure data loading time
        data_time.add(time.time() - end)
        inputs, target = inputs.to(args.device), target.to(args.device)

        # Execute the forward phase, compute the output and measure loss
        if compression_scheduler:
            compression_scheduler.on_minibatch_begin(epoch, train_step, steps_per_epoch, optimizer)

        if not hasattr(args, 'kd_policy') or args.kd_policy is None:
            output = model(inputs)
        else:
            output = args.kd_policy.forward(inputs)

        if not args.earlyexit_lossweights:
            loss = criterion(output, target)
            # Measure accuracy
            classerr.add(output.data, target)
            acc_stats.append([classerr.value(1), classerr.value(5)])
        else:
            # Measure accuracy and record loss
            loss = earlyexit_loss(output, target, criterion, args)
        # Record loss
        losses[OBJECTIVE_LOSS_KEY].add(loss.item())

        if compression_scheduler:
            # Before running the backward phase, we allow the scheduler to modify the loss
            # (e.g. add regularization loss)
            agg_loss = compression_scheduler.before_backward_pass(epoch, train_step,
                                                                  steps_per_epoch, loss,
                                                                  optimizer=optimizer,
                                                                  return_loss_components=True)
            loss = agg_loss.overall_loss
            losses[OVERALL_LOSS_KEY].add(loss.item())

            for lc in agg_loss.loss_components:
                if lc.name not in losses:
                    losses[lc.name] = tnt.AverageValueMeter()
                losses[lc.name].add(lc.value.item())
        else:
            losses[OVERALL_LOSS_KEY].add(loss.item())

        # Compute the gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        if compression_scheduler:
            compression_scheduler.before_parameter_optimization(epoch, train_step,
                                                                steps_per_epoch, optimizer)
        optimizer.step()
        if compression_scheduler:
            compression_scheduler.on_minibatch_end(epoch, train_step, steps_per_epoch, optimizer)

        # measure elapsed time
        batch_time.add(time.time() - end)
        steps_completed = (train_step+1)

        if steps_completed % args.print_freq == 0 or steps_completed == steps_per_epoch:
            # Log some statistics
            errs = OrderedDict()
            if not args.earlyexit_lossweights:
                errs['Top1'] = classerr.value(1)
                errs['Top5'] = classerr.value(5)
            else:
                # for Early Exit case, the Top1 and Top5 stats are computed for each exit.
                for exitnum in range(args.num_exits):
                    errs['Top1_exit' + str(exitnum)] = args.exiterrors[exitnum].value(1)
                    errs['Top5_exit' + str(exitnum)] = args.exiterrors[exitnum].value(5)

            stats_dict = OrderedDict()
            for loss_name, meter in losses.items():
                stats_dict[loss_name] = meter.mean
            stats_dict.update(errs)
            stats_dict['LR'] = optimizer.param_groups[0]['lr']
            stats_dict['Time'] = batch_time.mean
            stats = ('Performance/Training/', stats_dict)

            params = model.named_parameters() if args.log_params_histograms else None
            distiller.log_training_progress(stats,
                                            params,
                                            epoch, steps_completed,
                                            steps_per_epoch, args.print_freq,
                                            loggers)
        end = time.time()
    return acc_stats


def validate(val_loader, model, criterion, loggers, args, epoch=-1):
    """Model validation"""
    if epoch > -1:
        msglogger.info('--- validate (epoch=%d)-----------', epoch)
    else:
        msglogger.info('--- validate ---------------------')
    return _validate(val_loader, model, criterion, loggers, args, epoch)


def test(test_loader, model, criterion, loggers, activations_collectors, args):
    """Model Test"""
    msglogger.info('--- test ---------------------')
    if activations_collectors is None:
        activations_collectors = create_activation_stats_collectors(model, None)
    with collectors_context(activations_collectors["test"]) as collectors:
        top1, top5, lossses = _validate(test_loader, model, criterion, loggers, args)
        distiller.log_activation_statsitics(-1, "test", loggers, collector=collectors['sparsity'])

        if args.kernel_stats:
            print("==> Kernel Stats")
            with torch.no_grad():
                global weight_min, weight_max, weight_count, weight_sum, weight_stddev, weight_mean
                weight_min = torch.tensor(float('inf'))
                weight_max = torch.tensor(float('-inf'))
                weight_count = torch.tensor(0, dtype=torch.int)
                weight_sum = torch.tensor(0.0)
                weight_stddev = torch.tensor(0.0)

                def traverse_pass1(m):
                    """
                    Traverse model to build weight stats
                    """
                    global weight_min, weight_max, weight_count, weight_sum
                    if isinstance(m, nn.Conv2d):
                        weight_min = torch.min(torch.min(m.weight), weight_min)
                        weight_max = torch.max(torch.max(m.weight), weight_max)
                        weight_count += len(m.weight.flatten())
                        weight_sum += m.weight.flatten().sum()
                        if hasattr(m, 'bias') and m.bias is not None:
                            weight_min = torch.min(torch.min(m.bias), weight_min)
                            weight_max = torch.max(torch.max(m.bias), weight_max)
                            weight_count += len(m.bias.flatten())
                            weight_sum += m.bias.flatten().sum()

                def traverse_pass2(m):
                    """
                    Traverse model to build weight stats
                    """
                    global weight_stddev, weight_mean
                    if isinstance(m, nn.Conv2d):
                        weight_stddev += ((m.weight.flatten() - weight_mean) ** 2).sum()
                        if hasattr(m, 'bias') and m.bias is not None:
                            weight_stddev += ((m.bias.flatten() - weight_mean) ** 2).sum()

                model.apply(traverse_pass1)

                weight_mean = weight_sum / weight_count

                model.apply(traverse_pass2)

                weight_stddev = torch.sqrt(weight_stddev / weight_count)

                print(f"Total 2D kernel weights: {weight_count} --> min: {weight_min}, "
                      f"max: {weight_max}, stddev: {weight_stddev}")

        save_collectors_data(collectors, msglogger.logdir)
    return top1, top5, lossses


def _validate(data_loader, model, criterion, loggers, args, epoch=-1):
    """Execute the validation/test loop."""
    losses = {'objective_loss': tnt.AverageValueMeter()}
    classerr = tnt.ClassErrorMeter(accuracy=True, topk=(1, 5))

    if args.earlyexit_thresholds:
        # for Early Exit, we have a list of errors and losses for each of the exits.
        args.exiterrors = []
        args.losses_exits = []
        for exitnum in range(args.num_exits):
            args.exiterrors.append(tnt.ClassErrorMeter(accuracy=True, topk=(1, 5)))
            args.losses_exits.append(tnt.AverageValueMeter())
        args.exit_taken = [0] * args.num_exits

    batch_time = tnt.AverageValueMeter()
    total_samples = len(data_loader.sampler)
    batch_size = data_loader.batch_size
    if args.display_confusion:
        confusion = tnt.ConfusionMeter(args.num_classes)
    total_steps = total_samples / batch_size
    msglogger.info('%d samples (%d per mini-batch)', total_samples, batch_size)

    # Switch to evaluation mode
    model.eval()

    end = time.time()
    for validation_step, (inputs, target) in enumerate(data_loader):
        with torch.no_grad():
            inputs, target = inputs.to(args.device), target.to(args.device)
            # compute output from model
            output = model(inputs)

            if not args.earlyexit_thresholds:
                # compute loss
                loss = criterion(output, target)
                # measure accuracy and record loss
                losses['objective_loss'].add(loss.item())
                classerr.add(output.data, target)
                if args.display_confusion:
                    confusion.add(output.data, target)
            else:
                earlyexit_validate_loss(output, target, criterion, args)

            # measure elapsed time
            batch_time.add(time.time() - end)
            end = time.time()

            steps_completed = (validation_step+1)
            if steps_completed % args.print_freq == 0 or steps_completed == total_steps:
                if not args.earlyexit_thresholds:
                    stats = ('',
                             OrderedDict([('Loss', losses['objective_loss'].mean),
                                          ('Top1', classerr.value(1)),
                                          ('Top5', classerr.value(5))]))
                else:
                    stats_dict = OrderedDict()
                    stats_dict['Test'] = validation_step
                    for exitnum in range(args.num_exits):
                        la_string = 'LossAvg' + str(exitnum)
                        stats_dict[la_string] = args.losses_exits[exitnum].mean
                        # Because of the nature of ClassErrorMeter, if an exit is never taken
                        # during the batch, then accessing the value(k) will cause a divide by
                        # zero. So we'll build the OrderedDict accordingly and we will not print
                        # for an exit error when that exit is never taken.
                        if args.exit_taken[exitnum]:
                            t1 = 'Top1_exit' + str(exitnum)
                            t5 = 'Top5_exit' + str(exitnum)
                            stats_dict[t1] = args.exiterrors[exitnum].value(1)
                            stats_dict[t5] = args.exiterrors[exitnum].value(5)
                    stats = ('Performance/Validation/', stats_dict)

                distiller.log_training_progress(stats, None, epoch, steps_completed,
                                                total_steps, args.print_freq, loggers)
    if not args.earlyexit_thresholds:
        msglogger.info('==> Top1: %.3f    Top5: %.3f    Loss: %.3f\n',
                       classerr.value()[0], classerr.value()[1], losses['objective_loss'].mean)

        if args.display_confusion:
            msglogger.info('==> Confusion:\n%s\n', str(confusion.value()))
        return classerr.value(1), classerr.value(5), losses['objective_loss'].mean
    else:
        total_top1, total_top5, losses_exits_stats = earlyexit_validate_stats(args)
        return total_top1, total_top5, losses_exits_stats[args.num_exits-1]


def update_training_scores_history(perf_scores_history, model, top1, top5, epoch, num_best_scores):
    """ Update the list of top training scores achieved so far, and log the best scores so far"""

    model_sparsity, _, params_nnz_cnt = distiller.model_params_stats(model)
    perf_scores_history.append(distiller.MutableNamedTuple({'params_nnz_cnt': -params_nnz_cnt,
                                                            'sparsity': model_sparsity,
                                                            'top1': top1, 'top5': top5,
                                                            'epoch': epoch}))
    # Keep perf_scores_history sorted from best to worst
    # Sort by sparsity as main sort key, then sort by top1, top5 and epoch
    perf_scores_history.sort(key=operator.attrgetter('params_nnz_cnt', 'top1', 'top5', 'epoch'),
                             reverse=True)
    for score in perf_scores_history[:num_best_scores]:
        msglogger.info('==> Best [Top1: %.3f   Top5: %.3f   Sparsity:%.2f   '
                       'Params: %d on epoch: %d]',
                       score.top1, score.top5, score.sparsity, -score.params_nnz_cnt, score.epoch)


def earlyexit_loss(output, target, criterion, args):
    loss = 0
    sum_lossweights = 0
    for exitnum in range(args.num_exits-1):
        loss += (args.earlyexit_lossweights[exitnum] * criterion(output[exitnum], target))
        sum_lossweights += args.earlyexit_lossweights[exitnum]
        args.exiterrors[exitnum].add(output[exitnum].data, target)
    # handle final exit
    loss += (1.0 - sum_lossweights) * criterion(output[args.num_exits-1], target)
    args.exiterrors[args.num_exits-1].add(output[args.num_exits-1].data, target)
    return loss


def earlyexit_validate_loss(output, target, _criterion, args):
    # We need to go through each sample in the batch itself - in other words, we are
    # not doing batch processing for exit criteria - we do this as though it were batchsize of 1
    # but with a grouping of samples equal to the batch size.
    # Note that final group might not be a full batch - so determine actual size.
    this_batch_size = target.size()[0]
    earlyexit_validate_criterion = nn.CrossEntropyLoss(reduce=False).to(args.device)

    for exitnum in range(args.num_exits):
        # calculate losses at each sample separately in the minibatch.
        args.loss_exits[exitnum] = earlyexit_validate_criterion(output[exitnum], target)
        # for batch_size > 1, we need to reduce this down to an average over the batch
        args.losses_exits[exitnum].add(torch.mean(args.loss_exits[exitnum]).cpu())

    for batch_index in range(this_batch_size):
        earlyexit_taken = False
        # take the exit using CrossEntropyLoss as confidence measure (lower is more confident)
        for exitnum in range(args.num_exits - 1):
            if args.loss_exits[exitnum][batch_index] < args.earlyexit_thresholds[exitnum]:
                # take the results from early exit since lower than threshold
                args.exiterrors[exitnum].add(torch.tensor(np.array(output[exitnum].
                                             data[batch_index].cpu(), ndmin=2)),
                                             torch.full([1], target[batch_index],
                                             dtype=torch.long))
                args.exit_taken[exitnum] += 1
                earlyexit_taken = True
                break  # since exit was taken, do not affect the stats of subsequent exits
        # this sample does not exit early and therefore continues until final exit
        if not earlyexit_taken:
            exitnum = args.num_exits - 1
            args.exiterrors[exitnum].add(torch.tensor(np.array(output[exitnum].
                                         data[batch_index].cpu(), ndmin=2)),
                                         torch.full([1], target[batch_index], dtype=torch.long))
            args.exit_taken[exitnum] += 1


def earlyexit_validate_stats(args):
    # Print some interesting summary stats for number of data points that could exit early
    top1k_stats = [0] * args.num_exits
    top5k_stats = [0] * args.num_exits
    losses_exits_stats = [0] * args.num_exits
    sum_exit_stats = 0
    for exitnum in range(args.num_exits):
        if args.exit_taken[exitnum]:
            sum_exit_stats += args.exit_taken[exitnum]
            msglogger.info("Exit %d: %d", exitnum, args.exit_taken[exitnum])
            top1k_stats[exitnum] += args.exiterrors[exitnum].value(1)
            top5k_stats[exitnum] += args.exiterrors[exitnum].value(5)
            losses_exits_stats[exitnum] += args.losses_exits[exitnum].mean
    for exitnum in range(args.num_exits):
        if args.exit_taken[exitnum]:
            msglogger.info("Percent Early Exit %d: %.3f", exitnum,
                           (args.exit_taken[exitnum]*100.0) / sum_exit_stats)
    total_top1 = 0
    total_top5 = 0
    for exitnum in range(args.num_exits):
        total_top1 += (top1k_stats[exitnum] * (args.exit_taken[exitnum] / sum_exit_stats))
        total_top5 += (top5k_stats[exitnum] * (args.exit_taken[exitnum] / sum_exit_stats))
        msglogger.info("Accuracy Stats for exit %d: top1 = %.3f, top5 = %.3f", exitnum,
                       top1k_stats[exitnum], top5k_stats[exitnum])
    msglogger.info("Totals for entire network with early exits: top1 = %.3f, top5 = %.3f",
                   total_top1, total_top5)
    return total_top1, total_top5, losses_exits_stats


def evaluate_model(model, criterion, test_loader, loggers, activations_collectors, args,
                   scheduler=None):
    # This sample application can be invoked to evaluate the accuracy of your model on
    # the test dataset.
    # You can optionally quantize the model to 8-bit integer before evaluation.
    # For example:
    # python3 compress_classifier.py --arch resnet20_cifar \
    #          ../data.cifar10 -p=50 --resume-from=checkpoint.pth.tar --evaluate

    if not isinstance(loggers, list):
        loggers = [loggers]

    if args.quantize_eval:
        model.cpu()
        if args.ai84:
            quantizer = PostTrainLinearQuantizerAI84.from_args(model, args)
        else:
            quantizer = PostTrainLinearQuantizer.from_args(model, args)
        quantizer.prepare_model()
        model.to(args.device)

    top1, _, _ = test(test_loader, model, criterion, loggers, activations_collectors, args=args)

    if args.quantize_eval:
        checkpoint_name = 'quantized'
        apputils.save_checkpoint(0, args.cnn, model, optimizer=None, scheduler=scheduler,
                                 name='_'.join([args.name, checkpoint_name])
                                 if args.name else checkpoint_name,
                                 dir=msglogger.logdir, extras={'quantized_top1': top1})


def summarize_model(model, dataset, which_summary):
    if which_summary.startswith('png'):
        model_summaries.draw_img_classifier_to_file(model, 'model.png', dataset,
                                                    which_summary == 'png_w_params')
    elif which_summary == 'onnx':
        model_summaries.export_img_classifier_to_onnx(model, 'model.onnx', dataset)
    else:
        distiller.model_summary(model, which_summary, dataset)


def sensitivity_analysis(model, criterion, data_loader, loggers, args, sparsities):
    # This sample application can be invoked to execute Sensitivity Analysis on your
    # model.  The ouptut is saved to CSV and PNG.
    msglogger.info("Running sensitivity tests")
    if not isinstance(loggers, list):
        loggers = [loggers]
    test_fnc = partial(test, test_loader=data_loader, criterion=criterion,
                       loggers=loggers, args=args,
                       activations_collectors=create_activation_stats_collectors(model))
    which_params = [param_name for param_name, _ in model.named_parameters()]
    sensitivity = distiller.perform_sensitivity_analysis(model,
                                                         net_params=which_params,
                                                         sparsities=sparsities,
                                                         test_func=test_fnc,
                                                         group=args.sensitivity)
    distiller.sensitivities_to_png(sensitivity, 'sensitivity.png')
    distiller.sensitivities_to_csv(sensitivity, 'sensitivity.csv')


def automated_deep_compression(model, criterion, _optimizer, loggers, args):
    train_loader, _val_loader, test_loader, _ = apputils.get_data_loaders(
        args.datasets_fn, (os.path.expanduser(args.data), args), args.batch_size,
        args.workers, args.validation_split, args.deterministic,
        args.effective_train_size, args.effective_valid_size, args.effective_test_size)

    args.display_confusion = True
    validate_fn = partial(test, test_loader=test_loader, criterion=criterion,
                          loggers=loggers, args=args, activations_collectors=None)
    train_fn = partial(train, train_loader=train_loader, criterion=criterion,
                       loggers=loggers, args=args)

    save_checkpoint_fn = partial(apputils.save_checkpoint, arch=args.cnn, dir=msglogger.logdir)
    optimizer_data = {'lr': args.lr, 'momentum': args.momentum, 'weight_decay': args.weight_decay}
    adc.do_adc(model, args, optimizer_data, validate_fn, save_checkpoint_fn, train_fn)


def greedy(model, criterion, _optimizer, loggers, args):
    train_loader, _val_loader, test_loader, _ = apputils.get_data_loaders(
        args.datasets_fn, (os.path.expanduser(args.data), args), args.batch_size,
        args.workers, args.validation_split, args.deterministic,
        args.effective_train_size, args.effective_valid_size, args.effective_test_size)

    test_fn = partial(test, test_loader=test_loader, criterion=criterion,
                      loggers=loggers, args=args, activations_collectors=None)
    train_fn = partial(train, train_loader=train_loader, criterion=criterion, args=args)
    assert args.greedy_target_density is not None
    distiller.pruning.greedy_filter_pruning.greedy_pruner(model, args,
                                                          args.greedy_target_density,
                                                          args.greedy_pruning_step,
                                                          test_fn, train_fn)


class missingdict(dict):
    """This is a little trick to prevent KeyError"""
    def __missing__(self, key):
        return None  # note, does *not* set self[key] - we don't want defaultdict's behavior


def create_activation_stats_collectors(model, *phases):
    """Create objects that collect activation statistics.

    This is a utility function that creates two collectors:
    1. Fine-grade sparsity levels of the activations
    2. L1-magnitude of each of the activation channels

    Args:
        model - the model on which we want to collect statistics
        phases - the statistics collection phases: train, valid, and/or test

    WARNING! Enabling activation statsitics collection will significantly slow down training!
    """
    distiller.utils.assign_layer_fq_names(model)

    genCollectors = lambda: missingdict({  # noqa E731
        "sparsity":      SummaryActivationStatsCollector(model, "sparsity",
                                                         lambda t:
                                                         100 * distiller.utils.sparsity(t)),
        "l1_channels":   SummaryActivationStatsCollector(model, "l1_channels",
                                                         distiller.utils.activation_channels_l1),
        "apoz_channels": SummaryActivationStatsCollector(model, "apoz_channels",
                                                         distiller.utils.activation_channels_apoz),
        "mean_channels": SummaryActivationStatsCollector(model, "mean_channels",
                                                         distiller.utils.
                                                         activation_channels_means),
        "records":       RecordsActivationStatsCollector(model, classes=[torch.nn.Conv2d])
    })

    return {k: (genCollectors() if k in phases else missingdict())
            for k in ('train', 'valid', 'test')}


def create_quantization_stats_collector(model):
    distiller.utils.assign_layer_fq_names(model)
    return {'test': missingdict({
        'quantization_stats': QuantCalibrationStatsCollector(model, classes=None,
                                                             inplace_runtime_check=True,
                                                             disable_inplace_attrs=True)})}


def save_collectors_data(collectors, directory):
    """Utility function that saves all activation statistics to Excel workbooks
    """
    for name, collector in collectors.items():
        workbook = os.path.join(directory, name)
        msglogger.info("Generating {}".format(workbook))
        collector.save(workbook)


def check_pytorch_version():
    from pkg_resources import parse_version
    if parse_version(torch.__version__) < parse_version('1.0.1'):
        print("\nNOTICE:")
        print("The Distiller \'master\' branch now requires at least PyTorch version 1.0.1 due to "
              "PyTorch API changes which are not backward-compatible.\n"
              "Please install PyTorch 1.0.1 or its derivative.\n"
              "If you are using a virtual environment, do not forget to update it:\n"
              "  1. Deactivate the old environment\n"
              "  2. Install the new environment\n"
              "  3. Activate the new environment")
        exit(1)


class normalize(object):
    """
    Normalize input to either [-0.5, +0.5] or [-128, +127]
    """
    def __init__(self, args):
        self.args = args

    def __call__(self, img):
        if self.args.act_mode_8bit:
            return img.sub(0.5).mul(256.).round().clamp(min=-128, max=127)
        return img.sub(0.5)


def cifar10_get_datasets(data):
    """Load the CIFAR10 dataset.

    The original training dataset is split into training and validation sets (code is
    inspired by https://gist.github.com/kevinzakka/d33bf8d6c7f06a9d8c76d97a7879f5cb).
    By default we use a 90:10 (45K:5K) training:validation split.

    The output of torchvision datasets are PIL Image images of range [0, 1].
    We transform them to Tensors of normalized range [-1, 1]
    https://github.com/pytorch/tutorials/blob/master/beginner_source/blitz/cifar10_tutorial.py

    Data augmentation: 4 pixels are padded on each side, and a 32x32 crop is randomly sampled
    from the padded image or its horizontal flip.
    This is similar to [1] and some other work that use CIFAR10.

    [1] C.-Y. Lee, S. Xie, P. Gallagher, Z. Zhang, and Z. Tu. Deeply Supervised Nets.
    arXiv:1409.5185, 2014
    """
    (data_dir, args) = data

    train_transform = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize(args=args)
    ])

    train_dataset = torchvision.datasets.CIFAR10(root=os.path.join(data_dir, 'CIFAR10'),
                                                 train=True, download=True,
                                                 transform=train_transform)

    test_transform = transforms.Compose([
        transforms.ToTensor(),
        normalize(args=args)
    ])

    test_dataset = torchvision.datasets.CIFAR10(root=os.path.join(data_dir, 'CIFAR10'),
                                                train=False, download=True,
                                                transform=test_transform)

    if args.truncate_testset:
        test_dataset.data = test_dataset.data[:1]

    return train_dataset, test_dataset


def mnist_get_datasets(data):
    """Load the MNIST dataset.

    The original training dataset is split into training and validation sets (code is
    inspired by https://gist.github.com/kevinzakka/d33bf8d6c7f06a9d8c76d97a7879f5cb).
    By default we use a 90:10 (45K:5K) training:validation split.

    The output of torchvision datasets are PIL Image images of range [0, 1].
    We transform them to Tensors of normalized range [-1, 1]
    https://github.com/pytorch/tutorials/blob/master/beginner_source/blitz/cifar10_tutorial.py

    Data augmentation: 4 pixels are padded on each side, and a 32x32 crop is randomly sampled
    from the padded image or its horizontal flip.
    This is similar to [1] and some other work that use CIFAR10.

    [1] C.-Y. Lee, S. Xie, P. Gallagher, Z. Zhang, and Z. Tu. Deeply Supervised Nets.
    arXiv:1409.5185, 2014
    """
    (data_dir, args) = data

    train_transform = transforms.Compose([
        transforms.RandomCrop(28, padding=4),
        transforms.RandomAffine(degrees=20, translate=(0.1, 0.1), shear=5),
        transforms.ToTensor(),
        normalize(args=args)
    ])

    train_dataset = torchvision.datasets.MNIST(root=data_dir, train=True, download=True,
                                               transform=train_transform)

    test_transform = transforms.Compose([
        transforms.ToTensor(),
        normalize(args=args)
    ])

    test_dataset = torchvision.datasets.MNIST(root=data_dir, train=False, download=True,
                                              transform=test_transform)

    if args.truncate_testset:
        test_dataset.data = test_dataset.data[:1]

    return train_dataset, test_dataset


def fashionmnist_get_datasets(data):
    """Load the FashionMNIST dataset.

    The original training dataset is split into training and validation sets (code is
    inspired by https://gist.github.com/kevinzakka/d33bf8d6c7f06a9d8c76d97a7879f5cb).
    By default we use a 90:10 (45K:5K) training:validation split.

    The output of torchvision datasets are PIL Image images of range [0, 1].
    We transform them to Tensors of normalized range [-1, 1]
    https://github.com/pytorch/tutorials/blob/master/beginner_source/blitz/cifar10_tutorial.py

    Data augmentation: 4 pixels are padded on each side, and a 32x32 crop is randomly sampled
    from the padded image or its horizontal flip.
    This is similar to [1] and some other work that use CIFAR10.

    [1] C.-Y. Lee, S. Xie, P. Gallagher, Z. Zhang, and Z. Tu. Deeply Supervised Nets.
    arXiv:1409.5185, 2014
    """
    (data_dir, args) = data

    train_transform = transforms.Compose([
        transforms.RandomCrop(28, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize(args=args)
    ])

    train_dataset = torchvision.datasets.FashionMNIST(root=data_dir, train=True, download=True,
                                                      transform=train_transform)

    test_transform = transforms.Compose([
        transforms.ToTensor(),
        normalize(args=args)
    ])

    test_dataset = torchvision.datasets.FashionMNIST(root=data_dir, train=False, download=True,
                                                     transform=test_transform)

    if args.truncate_testset:
        test_dataset.data = test_dataset.data[:1]

    return train_dataset, test_dataset


if __name__ == '__main__':
    try:
        check_pytorch_version()
        np.set_printoptions(threshold=np.inf, linewidth=190)
        torch.set_printoptions(threshold=np.inf, linewidth=190)

        # import builtins, sys
        # print(distiller.config.__name__)
        # print(distiller.config.__builtins__)
        # # print(distiller.config.__import__)
        # builtins.QuantAwareTrainRangeLinearQuantizerAI84 = \
        #   range_linear_ai84.QuantAwareTrainRangeLinearQuantizerAI84
        # globals()['range_linear_ai84'] = __import__('range_linear_ai84')
        # sys.path.append('/home/robertmuchsel/Documents/Source/ai84')
        # site.addsitedir("/home/robertmuchsel/Documents/Source/ai84")
        main()
    except KeyboardInterrupt:
        print("\n-- KeyboardInterrupt --")
    except Exception:
        if msglogger is not None:
            # We catch unhandled exceptions here in order to log them to the log file
            # However, using the msglogger as-is to do that means we get the trace twice in
            # stdout - once from the logging operation and once from re-raising the exception.
            # So we remove the stdout logging handler before logging the exception
            handlers_bak = msglogger.handlers
            msglogger.handlers = [h for h in msglogger.handlers
                                  if type(h) != logging.StreamHandler]
            msglogger.error(traceback.format_exc())
            msglogger.handlers = handlers_bak
        raise
    finally:
        if msglogger is not None:
            msglogger.info('')
            msglogger.info('Log file for this run: ' + os.path.realpath(msglogger.log_filename))
