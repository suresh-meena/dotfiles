#!/bin/bash
#SBATCH --job-name=memconf
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=medium
#SBATCH --time=01:00:00
#SBATCH --mem=8G
#SBATCH --mem-per-cpu=2G
#SBATCH --ntasks=4

python3 worker.py
