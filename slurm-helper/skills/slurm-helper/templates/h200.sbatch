#!/bin/bash
#SBATCH --job-name={{job_name}}
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
#SBATCH --partition=h200
{{account_line}}#SBATCH --time={{time}}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{mem}}
{{gres_line}}{{qos_line}}#SBATCH --chdir={{workdir}}
{{strict_line}}
{{module_line}}mkdir -p logs

{{command}}
