#!/bin/sh
./train.py --model ai85kws20netv2 --dataset KWS_20 --confusion --evaluate --exp-load-weights-from ../ai8x-synthesis/trained/ai85-kws20-v2.pth.tar -8 --device MAX78000 $@