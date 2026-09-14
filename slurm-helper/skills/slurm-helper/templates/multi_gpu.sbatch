#!/bin/bash
#SBATCH --job-name={{job_name}}
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
#SBATCH --partition={{partition}}
#SBATCH --time={{time}}
#SBATCH --nodes={{nodes}}
#SBATCH --ntasks-per-node={{ntasks_per_node}}
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{mem}}
{{gres_line}}#SBATCH --chdir={{workdir}}
{{account_line}}{{strict_line}}
{{module_line}}mkdir -p logs

{{command}}
