#!/bin/bash
#SBATCH --job-name=badsweep
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.err
#SBATCH --partition=a100
#SBATCH --array=5-1
#SBATCH --time=02:00:00
#SBATCH --mem=8G

python3 sweep.py
