#!/bin/sh
python3 train.py --epochs 300 --optimizer Adam --lr 0.001 --compress schedule-cifar100.yaml --model ai85simplenetwide2x --dataset CIFAR100 --device MAX78000 --batch-size 100 --print-freq 100 --validation-split 0 --qat-policy qat_policy_cifar100.yaml --use-bias "$@"
