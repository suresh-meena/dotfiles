#!/bin/bash
#SBATCH --job-name={{job_name}}
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.err
#SBATCH --partition={{partition}}
{{array_line}}#SBATCH --time={{time}}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{mem}}
{{gres_line}}{{account_line}}#SBATCH --chdir={{workdir}}
{{strict_line}}
{{module_line}}mkdir -p logs

{{command}}
