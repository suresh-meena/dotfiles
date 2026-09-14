#!/bin/bash
#SBATCH --job-name=test_job
#SBATCH --output=test_job.out
#SBATCH --error=test_job.err
#SBATCH --partition=general
#SBATCH --ntasks=1
#SBATCH --time=04:00:00
#SBATCH --mem=16GB

echo "Hello, KIAC!"
