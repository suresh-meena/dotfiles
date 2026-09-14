#!/bin/bash
#SBATCH --job-name=maybe
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=short
#SBATCH --time=18:00:00
#SBATCH --mem=8G

python3 maybe.py
