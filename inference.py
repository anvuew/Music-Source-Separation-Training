# coding: utf-8
__author__ = 'Roman Solovyev (ZFTurbo): https://github.com/ZFTurbo/'

import argparse
import time
import librosa
from tqdm import tqdm
import sys
import os
import glob
import torch
import numpy as np
import soundfile as sf
import torch.nn as nn

# Using the embedded version of Python can also correctly import the utils module.
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
from utils import demix_track, demix_track_demucs, get_model_from_config

import warnings
warnings.filterwarnings("ignore")


def run_folder(model, args, config, device, verbose=False):
    start_time = time.time()
    model.eval()
    all_mixtures_path = glob.glob(args.input_folder + '/*.*')
    all_mixtures_path.sort()
    print('Total files found: {}'.format(len(all_mixtures_path)))

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)

    if not verbose:
        all_mixtures_path = tqdm(all_mixtures_path, desc="Total progress")

    if args.disable_detailed_pbar:
        detailed_pbar = False
    else:
        detailed_pbar = True

    for path in all_mixtures_path:
        print("Starting processing track: ", path)
        if not verbose:
            all_mixtures_path.set_postfix({'track': os.path.basename(path)})
        try:
            mix, sr = librosa.load(path, sr=44100, mono=False)
        except Exception as e:
            print('Cannot read track: {}'.format(path))
            print('Error message: {}'.format(str(e)))
            continue

        # Convert mono to stereo if needed
        if len(mix.shape) == 1:
            mix = np.stack([mix, mix], axis=0)

        mix_orig = mix.copy()
        if 'normalize' in config.inference:
            if config.inference['normalize'] is True:
                mono = mix.mean(0)
                mean = mono.mean()
                std = mono.std()
                mix = (mix - mean) / std

        if args.use_tta:
            # orig, channel inverse, polarity inverse
            track_proc_list = [mix.copy(), mix[::-1].copy(), -1. * mix.copy()]
        else:
            track_proc_list = [mix.copy()]

        full_result = []
        for single_track in track_proc_list:
            mixture = torch.tensor(single_track, dtype=torch.float32)
            if args.model_type == 'htdemucs':
                waveforms = demix_track_demucs(config, model, mixture, device, pbar=detailed_pbar)
            else:
                waveforms = demix_track(config, model, mixture, device, pbar=detailed_pbar)
            full_result.append(waveforms)

        # Average all values in single dict
        waveforms = full_result[0]
        for i in range(1, len(full_result)):
            d = full_result[i]
            for el in d:
                if i == 2:
                    waveforms[el] += -1.0 * d[el]
                elif i == 1:
                    waveforms[el] += d[el][::-1].copy()
                else:
                    waveforms[el] += d[el]
        for el in waveforms:
            waveforms[el] = waveforms[el] / len(full_result)

        for instr in instruments:
            estimates = waveforms[instr].T
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = estimates * std + mean
            file_name, _ = os.path.splitext(os.path.basename(path))
            if args.flac_file:
                output_file = os.path.join(args.store_dir, f"{file_name}_{instr}.flac")
                subtype = 'PCM_16' if args.pcm_type == 'PCM_16' else 'PCM_24'
                sf.write(output_file, estimates, sr, subtype=subtype)
            else:
                output_file = os.path.join(args.store_dir, f"{file_name}_{instr}.wav")
                sf.write(output_file, estimates, sr, subtype='FLOAT')

        # Output "instrumental", which is an inverse of 'vocals' (or first stem in list if 'vocals' absent)
        if args.extract_instrumental:
            if 'vocals' in instruments:
                estimates = waveforms['vocals'].T
            else:
                estimates = waveforms[instruments[0]].T
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = estimates * std + mean
            file_name, _ = os.path.splitext(os.path.basename(path))
            if args.flac_file:
                instrum_file_name = os.path.join(args.store_dir, f"{file_name}_instrumental.flac")
                subtype = 'PCM_16' if args.pcm_type == 'PCM_16' else 'PCM_24'
                sf.write(instrum_file_name, mix_orig.T - estimates, sr, subtype=subtype)
            else:
                instrum_file_name = os.path.join(args.store_dir, f"{file_name}_instrumental.wav")
                sf.write(instrum_file_name, mix_orig.T - estimates, sr, subtype='FLOAT')

    time.sleep(1)
    print("Elapsed time: {:.2f} sec".format(time.time() - start_time))


def proc_folder(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_type", type=str, default='mdx23c', help="One of bandit, bandit_v2, bs_roformer, htdemucs, mdx23c, mel_band_roformer, scnet, scnet_unofficial, segm_models, swin_upernet, torchseg")
    parser.add_argument("--config_path", type=str, help="path to config file")
    parser.add_argument("--start_check_point", type=str, default='', help="Initial checkpoint to valid weights")
    parser.add_argument("--input_folder", type=str, help="folder with mixtures to process")
    parser.add_argument("--store_dir", default="", type=str, help="path to store results as wav file")
    parser.add_argument("--device_ids", nargs='+', type=int, default=0, help='list of gpu ids')
    parser.add_argument("--extract_instrumental", action='store_true', help="invert vocals to get instrumental if provided")
    parser.add_argument("--disable_detailed_pbar", action='store_true', help="disable detailed progress bar")
    parser.add_argument("--force_cpu", action = 'store_true', help="Force the use of CPU even if CUDA is available")
    parser.add_argument("--flac_file", action = 'store_true', help="Output flac file instead of wav")
    parser.add_argument("--pcm_type", type=str, choices=['PCM_16', 'PCM_24'], default='PCM_24', help="PCM type for FLAC files (PCM_16 or PCM_24)")
    parser.add_argument("--use_tta", action='store_true', help="Flag adds test time augmentation during inference (polarity and channel inverse). While this triples the runtime, it reduces noise and slightly improves prediction quality.")
    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    device = "cpu"
    if args.force_cpu:
        device = "cpu"
    elif torch.cuda.is_available():
        print('CUDA is available, use --force_cpu to disable it.')
        device = "cuda"
        device = f'cuda:{args.device_ids}' if type(args.device_ids) == int else f'cuda:{args.device_ids[0]}'
    elif torch.backends.mps.is_available():
        device = "mps"

    print("Using device: ", device)

    model_load_start_time = time.time()
    torch.backends.cudnn.benchmark = True

    model, config = get_model_from_config(args.model_type, args.config_path)
    if args.start_check_point != '':
        print('Start from checkpoint: {}'.format(args.start_check_point))
        if args.model_type == 'htdemucs':
            state_dict = torch.load(args.start_check_point, map_location = device, weights_only=False)
            # Fix for htdemucs pretrained models
            if 'state' in state_dict:
                state_dict = state_dict['state']
        else:
            state_dict = torch.load(args.start_check_point, map_location = device, weights_only=True)
        model.load_state_dict(state_dict)
    print("Instruments: {}".format(config.training.instruments))

    # in case multiple CUDA GPUs are used and --device_ids arg is passed
    if type(args.device_ids) != int:
        model = nn.DataParallel(model, device_ids = args.device_ids)

    model = model.to(device)

    print("Model load time: {:.2f} sec".format(time.time() - model_load_start_time))

    run_folder(model, args, config, device, verbose=True)


if __name__ == "__main__":
    proc_folder(None)
