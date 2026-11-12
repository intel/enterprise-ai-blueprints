# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for query classification."""

import pytest
from models import QueryClassification


def test_query_classification_model_cat1():
    c = QueryClassification(category=1, reasoning="Simple fact", search_queries=[])
    assert c.category == 1
    assert c.search_queries == []


def test_query_classification_model_cat2():
    c = QueryClassification(category=2, reasoning="Needs lookup", search_queries=["test query"])
    assert c.category == 2
    assert len(c.search_queries) == 1


def test_query_classification_model_cat3():
    c = QueryClassification(category=3, reasoning="Needs research", search_queries=["q1", "q2", "q3"])
    assert c.category == 3
    assert len(c.search_queries) == 3


def test_query_classification_rejects_invalid_category():
    with pytest.raises(Exception):
        QueryClassification(category=0, reasoning="Bad", search_queries=[])
    with pytest.raises(Exception):
        QueryClassification(category=4, reasoning="Bad", search_queries=[])
