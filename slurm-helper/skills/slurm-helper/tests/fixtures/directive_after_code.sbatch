#!/bin/bash
#SBATCH --job-name=late
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=short
#SBATCH --time=00:30:00

echo start
#SBATCH --mem=8G
