""" Dataset class for Reddit EL dataset. """

import csv
from typing import Set, List, Tuple, Dict

from spacy.tokens import Doc

from schemas import Entity, Annotation
from .dataset import Dataset
from .utils import _fetch_entity_information, _create_spans_from_doc_annotation
from utils import get_logger

logger = get_logger(__name__)


class RedditDataset(Dataset):
    """RedditEL dataset."""

    def __init__(self):
        super().__init__()
        assert (
            self._options["gold"] or self._options["silver"] or self._options["bronze"]
        ), "At least one out of (gold, silver, bronze) has to be enabled."
        assert (
            self._options["posts"] or self._options["comments"]
        ), "At least one out of (posts, comments) has to be enabled."

    @property
    def name(self) -> str:
        return "reddit"

    def _parse_external_corpus(
        self,
    ) -> Tuple[Dict[str, Entity], Set[str], Dict[str, List[Annotation]]]:
        file_names = [
            f"{quality}_{source[:-1]}_annotations.tsv"
            for quality in ("gold", "silver", "bronze")
            for source in ("posts", "comments")
            if self._options[quality] and self._options[source]
        ]
        rows: List[List[str]] = []
        entity_names: Set[str] = set()
        annotations: Dict[str, List[Annotation]] = {}

        # Load data from .tsv files, track entity frequency.
        for file_name in file_names:
            with open(self._paths["assets"] / file_name, encoding="utf-8") as file_path:
                for i, row in enumerate(csv.reader(file_path, delimiter="\t")):
                    assert len(row) == 7
                    # Ditch anchor information in article URLs, as we can't use this in Wikidata lookups anyway.
                    row[3] = row[3].split("#")[0].split("?")[0]
                    rows.append(row)
                    entity_names.add(row[3])
                    if row[0] not in annotations:
                        annotations[row[0]] = []
                    annotations[row[0]].append(
                        Annotation(
                            entity_name=row[3],
                            start_pos=int(row[4]),
                            end_pos=int(row[5]),
                        )
                    )

        # Fetch Wikidata IDs (QIDs). Some entities won't be resolved properly because of messy situations with redirects
        # and normalizations (e.g.: two different titles are redirected to the same entity, Wikipedia only returns this
        # one entity. Associating the remaining title with the correct entity can bloat up the code).
        # Since we don't expect many failures, we instead run failed lookups again individually. This should avoid any
        # situations with entity interdependencies at the cost of lookup speed.
        entities, failed_entity_lookups, title_qid_mappings = _fetch_entity_information(
            "name", tuple(entity_names)
        )

        # Update mentions with corresponding entity IDs.
        for source_id in annotations:
            for annotation in annotations[source_id]:
                if annotation.entity_name not in failed_entity_lookups:
                    annotation.entity_id = title_qid_mappings[annotation.entity_name]

        return entities, failed_entity_lookups, annotations

    def _create_annotated_docs(self) -> List[Doc]:
        annotated_docs: List[Doc] = []
        file_names: List[str] = []
        if self._options["posts"]:
            file_names.append("posts.tsv")
        if self._options["comments"]:
            file_names.append("comments.tsv")
        msg = "Either 'posts' or 'comments' have to be True in corpus config."
        assert file_names, msg

        # Join records with line breaks.
        rows: List[List[str]] = []
        for file_name in [
            self._paths["assets"] / file_name for file_name in file_names
        ]:
            row_length = 3 if file_name.name.endswith("posts.tsv") else 5
            with open(file_name, encoding="utf-8") as file_path:
                for row in csv.reader(file_path, delimiter="\t"):
                    assert len(row) <= row_length
                    # If row has fewer than the specified number of entries: newlines from comments have been
                    # maintained, content is part of last valid comment.
                    if 0 < len(row) < row_length:
                        assert len(row) <= 1
                        rows[-1][-1] += " " + row[0]
                    elif len(row) == row_length:
                        rows.append(row)

        # Create spans from annotations.
        for row in rows:
            doc = self._nlp_base(row[-1])
            # There might be multiple annotations for the same tokens/spans. This is handled by (1) sorting all
            # entities for this document by their frequency and (2) afterwards moving all overlapping entities to
            # the doc's _ attribute, so we might still consider that during evaluation.
            # Additionally, there is a number of index errors in the annotations (especially in the bronze dataset).
            # Some of these are resolved by aligning annotation with token indices.
            doc.ents, _ = _create_spans_from_doc_annotation(
                doc=doc,
                entities_info=self._entities,
                annotations=self._annotations.get(row[0], []),
                entities_failed_lookups=self._failed_entity_lookups,
            )
            annotated_docs.append(doc)

        return annotated_docs

    def clean_assets(self) -> None:
        to_remove = {"bronze_comment_annotations.tsv": {}}
        to_replace = {
            "bronze_comment_annotations.tsv": {
                "`` How to Lose a Guy in 10 Days": "How to Lose a Guy in 10 Days",
                '"How to Lose a Guy in 10 Days': '"How to Lose a Guy in 10 Days"',
                "Money\t325\t330\tmoney\n": "Money\t323\t328\tmoney\n",
            }
        }
        encoding = "utf-8"

        for file_name in to_replace:
            lines: List[str] = []
            with open(
                self._paths["assets"] / file_name, "r", encoding=encoding
            ) as file:
                for line in file:
                    if line not in to_remove[file_name]:
                        for snippet in to_replace[file_name]:
                            if snippet in line:
                                line = line.replace(
                                    snippet, to_replace[file_name][snippet]
                                )
                        lines.append(line)

            (self._paths["assets"] / file_name).write_text(
                "".join(lines), encoding=encoding
            )
