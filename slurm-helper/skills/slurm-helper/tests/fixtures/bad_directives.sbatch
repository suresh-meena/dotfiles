#!/bin/bash
#SBATCH --job-name=kitchen
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=long
#SBATCH --partition=short
#SBATCH --nodelists=cn1
#SBATCH --time=00:30:00
#SBATCH --ntasks=0
#SBATCH --mem=8G
#SBATCH --dependency=foo:12
#SBATCH --gres=gpu:a100:0

echo hi
