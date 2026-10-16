# LinkedIn post draft — AGI Maze (arXiv:2607.00627)

I’m glad to share our new paper on **AGI Maze** — a lightweight framework for benchmarking **world-modeling agents**:
https://arxiv.org/abs/2607.00627

A recurring limitation of today’s LLM-based agents is not just “lack of intelligence” in the abstract, but the absence of a reliable mechanism for **maintaining and manipulating a latent world state** over time. In many real tasks, an agent must:
- compress experience into a usable **working memory**,
- form hypotheses about **hidden state** in partially observable environments,
- and **reason** over those representations under uncertainty and limited budgets.

**AGI Maze** targets exactly this gap. It is a family of grid maze tasks with a clean API where the agent receives only fragmented observations and must actively **reconstruct the state of the environment** (often akin to localization + mapping) in order to act efficiently. The environment includes mechanics such as rivers and pit cycles that create state aliasing and nontrivial transition dynamics under partial observability, breaking naive exploration.

On top of the core mechanics, AGI Maze supports extensions that introduce new items/actions and cell types. Private hidden sets are meant to help validate generality rather than specialization to the public dataset.

One of the main takeaways: unlike AGI-ARC-3, where strong LLM/tooling approaches can go quite far, modern “vanilla” LLM agents are still surprisingly weak at solving even small mazes reliably. Allowing an agent to use the prompt/history as a crude working memory helps, but the gains are limited without explicit mechanisms for external world models.

For a human-friendly entry point (rules + play mode), visit http://agimaze.org/. For the API specification and baseline agents and demo benchmarking tools, see https://github.com/Necr0x0Der/agimaze-bench.

AGI Maze is intended as a framework that can be used to create different benchmarks depending on the research goal — not only evaluations of pretrained LLM agents (with different harnesses for tool use, memory, planning, and explicit world modeling), but also lightweight training and smoke-testing of original architectures and RL agents. Feedback and suggestions are very welcome.
