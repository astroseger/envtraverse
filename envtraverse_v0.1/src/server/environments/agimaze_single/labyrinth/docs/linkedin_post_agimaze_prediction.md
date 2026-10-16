# LinkedIn post draft — AGI Maze Prediction Datasets (arXiv:2609.02339)

I’m glad to share our new paper, **AGI Maze Prediction Datasets: A Compact Benchmark for Learning World Dynamics with Transformers**:
https://arxiv.org/abs/2609.02339

A central question in world modeling is not only whether a model can form a latent representation of its environment, but **what form that representation takes—and what computations the architecture can perform over it**.

There is a useful analogy with vision. A fully connected perceptron must effectively learn the same object again and again at different image locations. Convolutional networks build translation-related structure into the computation, and are therefore far more parameter- and data-efficient for this setting.

Something similar may apply to reasoning. A standard Transformer can express and describe many rules in text, but its sequential token representation may be a poor computational substrate for some kinds of structured state: maps, graphs, object relations, or dynamically evolving environments. More data and parameters may help, but they need not be the most efficient way to obtain the relevant invariants and operations.

In our experiments, a conventional byte-level Transformer and an unstructured latent-memory variant both struggle as spatial prediction tasks become more demanding. By contrast, a model with a learned two-dimensional working memory reaches perfect held-out accuracy on selected multi-step tasks under the same data and evaluation protocol. It receives no intermediate state maps or state labels: the spatial workspace must become useful through learning.

The point is **not** that future world models should be hard-wired around two-dimensional maps. Spatial memory is only one example. The broader hypothesis is that language grounding and world modeling require architectures able to construct *arbitrary task-appropriate data structures* and carry out computations over them. Language can then serve as an interface to persistent, manipulable state—not as the only place where that state exists.

AGI Maze Prediction is intended as a small, controlled benchmark for testing this hypothesis before moving to richer environments and larger language-model architectures.
