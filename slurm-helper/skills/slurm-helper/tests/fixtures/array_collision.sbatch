#!/bin/bash
#SBATCH --job-name=sweep
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
#SBATCH --partition=a100
#SBATCH --array=1-8
#SBATCH --time=02:00:00
#SBATCH --mem=8G
#SBATCH --gres=gpu:1

python3 sweep.py
