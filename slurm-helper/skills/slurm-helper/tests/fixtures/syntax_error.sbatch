#!/bin/bash
#SBATCH --job-name=bad_syntax
#SBATCH --output=logs/%x_%j.out
#SBATCH --partition=short
#SBATCH --time=00:30:00
#SBATCH --mem=4G

if [ -f data.csv ]; then
  echo "found"
