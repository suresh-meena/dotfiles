#!/bin/bash
#SBATCH --job-name=toolong
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=long
#SBATCH --time=72:00:00
#SBATCH --mem=8G

python3 longrun.py
