# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, List, Optional, Type, Union

from nemo_curator.datasets import DocumentDataset
from nemo_curator.modules.base import Module
from nemo_curator.utils.module_utils import is_batched


class FilterMode(Enum):
    SCORE_FILTER = "score_filter"
    SCORE = "score"
    FILTER = "filter"


class DocumentFilter(Module, ABC):
    """
    An abstract base class for text-based document filters.

    This class serves as a template for creating specific document filters
    in the library. Subclasses should implement the abstract methods to
    define custom filtering behavior.
    """

    def __init__(
        self,
        score_type: Union[str, Type],
        text_fields: List[str] = ["text"],
        score_fields: List[str] = ["score"],
        filter_mode: FilterMode = FilterMode.SCORE_FILTER,
        removed_path: Optional[str] = None,
        invert: bool = False,
        save_score: bool = True,
        input_backend: str = "pandas",
    ):
        """
        text_fields: If len(text_fields) == 1, then score_document will
            get a series instead of a dataframe. You may still output
            multiple scores in the form of a dataframe. Need to verify if that's possible with Dask though.
        score_fields: If len(score_fields) == 1, then score_document
            must output a series instead of a dataframe. keep_document
            must accept a series instead of a dataframe. keep_document must always return a series.
        """
        super().__init__(input_backend=input_backend)
        self.score_type = score_type
        self.text_fields = text_fields
        self.score_fields = score_fields
        self.removed_path = removed_path
        self.invert = invert
        self.filter_mode = filter_mode
        self.save_score = save_score

    @abstractmethod
    def score_document(self, text: str) -> Any:
        """
        Calculate a score for the given document text.

        This method should be implemented by subclasses to define how
        a document's text is evaluated and scored.

        Args:
            text (str): The text content of the document to be scored.

        Returns:
            Any: A score or set of scores representing the document's
            relevance or quality. The type and structure of the
            return value should be consistent for each subclass.

        Raises:
            NotImplementedError: If the method is not implemented in a subclass.
        """
        raise NotImplementedError(
            "score_document method must be implemented by subclasses"
        )

    @abstractmethod
    def keep_document(self, scores: Any) -> bool:
        """
        Determine whether to keep a document based on its scores.

        This method should be implemented by subclasses to define the
        criteria for keeping or discarding a document based on the
        scores calculated by score_document().

        Args:
            scores (Any): The score or set of scores returned by score_document().
                          The type should match what is returned by score_document().

        Returns:
            bool: True if the document should be kept, False otherwise.

        Raises:
            NotImplementedError: If the method is not implemented in a subclass.
        """
        raise NotImplementedError(
            "keep_document method must be implemented by subclasses"
        )

    def _score_dataset(self, dataset: DocumentDataset):
        meta = (None, self.score_type)
        # Get the field name directly if there's only one
        text_fields = (
            self.text_fields if len(self.text_fields) > 1 else self.text_fields[0]
        )

        if is_batched(self.score_document):
            scores = dataset.df[text_fields].map_partitions(
                self.score_document, meta=meta
            )
        else:
            scores = dataset.df[text_fields].apply(
                self.score_document, axis=1, meta=meta
            )

        if self.save_score:
            score_fields = (
                self.score_fields
                if len(self.score_fields) > 1
                else self.score_fields[0]
            )
            dataset.df[score_fields] = scores

        return scores

    def _filter_dataset(self, dataset: DocumentDataset, scores):
        if is_batched(self.keep_document):
            bool_mask = scores.map_partitions(self.keep_document, meta=(None, bool))
        else:
            bool_mask = scores.apply(self.keep_document, axis=1, meta=(None, bool))
        if self.invert:
            bool_mask = ~bool_mask

        if self.removed_path:
            removed_docs = DocumentDataset(dataset.df[~bool_mask])
            removed_docs.to_parquet(output_file_dir=self.removed_path)

        return bool_mask

    def compute_filter_mask(self, dataset: DocumentDataset):
        scores = self._score_dataset(dataset)
        return self._filter_dataset(dataset, scores)

    def call(self, dataset: DocumentDataset) -> DocumentDataset:
        match self.filter_mode:
            case FilterMode.SCORE:
                self._score_dataset(dataset)
                return dataset
            case FilterMode.FILTER:
                score_fields = (
                    self.score_fields
                    if len(self.score_fields) > 1
                    else self.score_fields[0]
                )
                scores = dataset.df[score_fields]
                mask = self._filter_dataset(dataset, scores)
                return DocumentDataset(dataset.df[mask])
            case FilterMode.SCORE_FILTER:
                mask = self.compute_filter_mask(dataset)
                return DocumentDataset(dataset.df[mask])


def import_filter(filter_path: str) -> DocumentFilter:
    """
    Imports a filter under nemo_curator.filters given the module path

    Args:
        filter_path (str): The path to the filter in the form of "nemo_curator.filters.filter_name"

    Returns:
        DocumentFilter: The filter that is at the given path

    Raises:
        ValueError: If the filter_path does not point to a DocumentFilter
    """
    module_path, filter_name = filter_path.rsplit(".", 1)
    filter_module = importlib.import_module(module_path)
    filter_class = getattr(filter_module, filter_name)
    if not issubclass(filter_class, DocumentFilter):
        raise ValueError(
            f"Input filter {filter_class.__name__} must be derived "
            "from DocumentFilter defined in nemo_curator.filters.doc_filter"
        )
    return filter_class
