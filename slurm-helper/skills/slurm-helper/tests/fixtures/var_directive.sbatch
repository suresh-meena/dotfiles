#!/bin/bash
#SBATCH --job-name=dyn
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=medium
#SBATCH --time=01:00:00
#SBATCH --mem=${MEM}

python3 task.py
