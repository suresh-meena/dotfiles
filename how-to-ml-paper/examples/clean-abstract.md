# Clean abstract — expected: 0 strict-house findings

Retrieval augments a frozen language model with passages from Wikipedia. We train the retriever with contrastive supervision on Natural Questions. Retrieval raises exact match from 41.2 to 46.8 on the test split across three random seeds, with less than 2 percent added latency. Accuracy gains persist under distribution shift to TriviaQA, although the margin narrows to 1.9 points. We release code and preprocessing scripts to support reproduction.
