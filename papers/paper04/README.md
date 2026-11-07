# Supporting materials for “Can Environment-Agnostic Agents Solve ARC-AGI-3 from Warped and Noisy Images?”

**Paper:** arXiv link coming soon.

**Temporary article link:** [Google Drive](https://drive.google.com/file/d/1kD4KvfYtYykf_auaQtKzwN4SK8R-CntV/view?usp=sharing)

## Code and agents

See the [EnvTraverse v0.1 README](../../envtraverse_v0.1/README.md) for framework
setup and usage.

## Results and configurations

The [Zenodo record](https://zenodo.org/records/24021388) contains:

- `main_study_results_and_configs.tar.gz` — Per-game action records, scorecards,
  summary tables, and configurations for rerunning the main-study experiments
  with EnvTraverse.
- `followup_study_results_and_configs.tar.gz` — The same materials for the
  followup-study experiments.

## Full run artifacts

The complete `lf52` run under strong warping and extreme pixel noise
(σ = 1200), examined in the paper, is available on
[Zenodo](https://zenodo.org/records/24021388) as
`followup_sigma1200_lf52_run.tar.gz`. The agent's workspace, including
generated scripts and processed images, is in `run/agent_run`; raw observations
are in `observations`.

The complete run artifacts for the main and followup studies total approximately
700 GB, largely because noisy observation images compress poorly. Hosting the
entire collection for public download is impractical, but we can share the full
collection or any subset on request. Please contact the author to arrange access.

A [reduced main-study archive (57 GB)](https://www.dropbox.com/scl/fi/ag5ozz4jfct5fo2jzou8c/full_runs_no_jpg_after_step_10.tar.xz?rlkey=t0ijvgl88oird0ttamiun48q4&st=qoh3een0&dl=0)
is available on Dropbox. It contains the main-study run artifacts, with only
JPEG observation files after step 10 omitted.

## Citation

If you use this code or the released results, please cite the paper.
