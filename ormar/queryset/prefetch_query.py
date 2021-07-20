from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Sequence,
    Set,
    TYPE_CHECKING,
    Tuple,
    Type,
    Union,
    cast,
)

import ormar
from ormar.queryset.clause import QueryClause
from ormar.queryset.query import Query
from ormar.queryset.utils import extract_models_to_dict_of_lists, translate_list_to_dict

if TYPE_CHECKING:  # pragma: no cover
    from ormar import Model
    from ormar.fields import ForeignKeyField, BaseField
    from ormar.queryset import OrderAction
    from ormar.models.excludable import ExcludableItems


def sort_models(models: List["Model"], orders_by: Dict) -> List["Model"]:
    """
    Since prefetch query gets all related models by ids the sorting needs to happen in
    python. Since by default models are already sorted by id here we resort only if
    order_by parameters was set.

    :param models: list of models already fetched from db
    :type models: List[tests.test_prefetch_related.Division]
    :param orders_by: order by dictionary
    :type orders_by: Dict[str, str]
    :return: sorted list of models
    :rtype: List[tests.test_prefetch_related.Division]
    """
    sort_criteria = [
        (key, value) for key, value in orders_by.items() if isinstance(value, str)
    ]
    sort_criteria = sort_criteria[::-1]
    for criteria in sort_criteria:
        key, value = criteria
        if value == "desc":
            models.sort(key=lambda x: getattr(x, key), reverse=True)
        else:
            models.sort(key=lambda x: getattr(x, key))
    return models


def set_children_on_model(  # noqa: CCR001
    model: "Model",
    related: str,
    children: Dict,
    model_id: int,
    models: Dict,
    orders_by: Dict,
) -> None:
    """
    Extract ids of child models by given relation id key value.

    Based on those ids the actual children model instances are fetched from
    already fetched data.

    If needed the child models are resorted according to passed orders_by dict.

    Also relation is registered as each child is set as parent related field name value.

    :param model: parent model instance
    :type model: Model
    :param related: name of the related field
    :type related: str
    :param children: dictionary of children ids/ related field value
    :type children: Dict[int, set]
    :param model_id: id of the model on which children should be set
    :type model_id: int
    :param models: dictionary of child models instances
    :type models: Dict
    :param orders_by: order_by dictionary
    :type orders_by: Dict
    """
    if isinstance(model_id, dict):
        model_id = tuple(sorted((k, v) for k, v in model_id.items()))
    for key, child_models in children.items():
        if key == model_id:
            models_to_set = [models[child] for child in sorted(child_models)]
            if models_to_set:
                if orders_by and any(isinstance(x, str) for x in orders_by.values()):
                    models_to_set = sort_models(
                        models=models_to_set, orders_by=orders_by
                    )
                for child in models_to_set:
                    setattr(model, related, child)


class UniqueList(list):
    def append(self, item: Any) -> None:
        if item not in self:
            super().append(item)


@dataclass
class ExtractedEntity:
    prefix: str = ""
    models: Dict = field(default_factory=dict)
    pk_models: Dict = field(default_factory=dict)
    raw: List = field(default_factory=list)

    def get(self, name: str):
        if name not in self.models:
            self.models[name] = dict()
        return self.models[name]


class EntityExtractor:
    def __init__(self):
        self.entities = dict()

    def get(self, name: str):
        if name not in self.entities:
            self.entities[name] = ExtractedEntity()
        return self.entities[name]


class PrefetchQuery:
    """
    Query used to fetch related models in subsequent queries.
    Each model is fetched only ones by the name of the relation.
    That means that for each prefetch_related entry next query is issued to database.
    """

    def __init__(  # noqa: CFQ002
        self,
        model_cls: Type["Model"],
        excludable: "ExcludableItems",
        prefetch_related: List,
        select_related: List,
        orders_by: List["OrderAction"],
    ) -> None:

        self.model = model_cls
        self.database = self.model.Meta.database
        self._prefetch_related = prefetch_related
        self._select_related = select_related
        self.excludable = excludable
        self.already_extracted: EntityExtractor = EntityExtractor()
        self.models: Dict = {}
        self.select_dict = translate_list_to_dict(self._select_related)
        self.orders_by = orders_by or []
        # TODO: refactor OrderActions to use it instead of strings from it
        self.order_dict = translate_list_to_dict(
            [x.query_str for x in self.orders_by], is_order=True
        )

    async def prefetch_related(
        self, models: Sequence["Model"], rows: List
    ) -> Sequence["Model"]:
        """
        Main entry point for prefetch_query.

        Receives list of already initialized parent models with all children from
        select_related already populated. Receives also list of row sql result rows
        as it's quicker to extract ids that way instead of calling each model.

        Returns list with related models already prefetched and set.

        :param models: list of already instantiated models from main query
        :type models: List[Model]
        :param rows: row sql result of the main query before the prefetch
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :return: list of models with children prefetched
        :rtype: List[Model]
        """
        self.models = extract_models_to_dict_of_lists(
            model_type=self.model, models=models, select_dict=self.select_dict
        )
        self.models[self.model.get_name()] = models
        return await self._prefetch_related_models(models=models, rows=rows)

    def _extract_ids_from_raw_data(
        self, parent_model: Type["Model"], column_names: Union[str, List[str]]
    ) -> List:
        """
        Iterates over raw rows and extract id values of relation columns by using
        prefixed column name.

        :param parent_model: ormar model class
        :type parent_model: Type[Model]
        :param column_names: name of the relation column which is a key column
        :type column_names: str
        :return: set of ids of related model that should be extracted
        :rtype: set
        """
        if not isinstance(column_names, list):
            column_names = [column_names]
        list_of_ids = UniqueList()
        current_data = self.already_extracted.get(parent_model.get_name())
        table_prefix = current_data.prefix

        column_names = [
            (f"{table_prefix}_" if table_prefix else "") + column_name
            for column_name in column_names
        ]
        for row in current_data.raw:
            if all(row[column_name] for column_name in column_names):
                if len(column_names) > 1:
                    list_of_ids.append(
                        {column_name: row[column_name] for column_name in column_names}
                    )
                else:
                    list_of_ids.append(row[column_names[0]])
        return list_of_ids

    def _extract_ids_from_preloaded_models(
        self, parent_model: Type["Model"], column_name: Union[str, List[str]]
    ) -> List:
        """
        Extracts relation ids from already populated models if they were included
        in the original query before.

        :param parent_model: model from which related ids should be extracted
        :type parent_model: Type["Model"]
        :param column_name: name of the relation column which is a key column
        :type column_name: str
        :return: set of ids of related model that should be extracted
        :rtype: set
        """
        list_of_ids = UniqueList()
        for model in self.models.get(parent_model.get_name(), []):
            if isinstance(column_name, list):
                current_id = dict()
                for column in column_name:
                    column = model.get_column_name_from_alias(column)
                    child = getattr(model, column)
                    if isinstance(child, ormar.Model):
                        child = child.pk
                    if isinstance(child, dict):
                        field = parent_model.Meta.model_fields[column]
                        for target_name, own_name in field.names.items():
                            current_id[own_name] = child.get(target_name)
                    else:
                        current_id[model.get_column_alias(column)] = child
                list_of_ids.append(current_id)
            else:
                child = getattr(model, column_name)
                if isinstance(child, ormar.Model):
                    list_of_ids.append(child.pk)
                else:
                    list_of_ids.append(child)
        return list_of_ids

    def _extract_required_ids(
        self, parent_model: Type["Model"], reverse: bool, related: str,
    ) -> List:
        """
        Delegates extraction of the fields to either get ids from raw sql response
        or from already populated models.

        :param parent_model: model from which related ids should be extracted
        :type parent_model: Type["Model"]
        :param reverse: flag if the relation is reverse
        :type reverse: bool
        :param related: name of the field with relation
        :type related: str
        :return: set of ids of related model that should be extracted
        :rtype: set
        """
        use_raw = parent_model.get_name() not in self.models

        column_names = parent_model.get_column_name_for_id_extraction(
            parent_model=parent_model,
            reverse=reverse,
            related=related,
            use_raw=use_raw,
        )

        if use_raw:
            return self._extract_ids_from_raw_data(
                parent_model=parent_model, column_names=column_names
            )

        return self._extract_ids_from_preloaded_models(
            parent_model=parent_model, column_name=column_names
        )

    def _get_filter_for_prefetch(
        self,
        parent_model: Type["Model"],
        target_model: Type["Model"],
        reverse: bool,
        related: str,
    ) -> List:
        """
        Populates where clause with condition to return only models within the
        set of extracted ids.

        If there are no ids for relation the empty list is returned.

        :param parent_model: model from which related ids should be extracted
        :type parent_model: Type["Model"]
        :param target_model: model to which relation leads to
        :type target_model: Type["Model"]
        :param reverse: flag if the relation is reverse
        :type reverse: bool
        :param related: name of the field with relation
        :type related: str
        :return:
        :rtype: List[sqlalchemy.sql.elements.TextClause]
        """
        ids = self._extract_required_ids(
            parent_model=parent_model, reverse=reverse, related=related
        )
        if ids:
            (
                clause_target,
                filter_column,
            ) = parent_model.get_clause_target_and_filter_column_name(
                parent_model=parent_model,
                target_model=target_model,
                reverse=reverse,
                related=related,
            )
            qryclause = QueryClause(
                model_cls=clause_target, select_related=[], filter_clauses=[],
            )
            if isinstance(filter_column, dict):
                kwargs: Dict[str, Union[List, Set]] = dict()
                for own_name, target_name in filter_column.items():
                    kwargs[f"{own_name}__in"] = set(x.get(target_name) for x in ids)
            else:
                kwargs = {f"{cast(str, filter_column)}__in": ids}
            filter_clauses, _ = qryclause.prepare_filter(_own_only=False, **kwargs)
            return filter_clauses
        return []

    def _populate_nested_related(
        self, model: "Model", prefetch_dict: Dict, orders_by: Dict,
    ) -> "Model":
        """
        Populates all related models children of parent model that are
        included in prefetch query.

        :param model: ormar model instance
        :type model: Model
        :param prefetch_dict: dictionary of models to prefetch
        :type prefetch_dict: Dict
        :param orders_by: dictionary of order bys
        :type orders_by: Dict
        :return: model with children populated
        :rtype: Model
        """
        related_to_extract = model.get_filtered_names_to_extract(
            prefetch_dict=prefetch_dict
        )

        for related in related_to_extract:
            target_field = model.Meta.model_fields[related]
            target_field = cast("ForeignKeyField", target_field)
            target_model = target_field.to.get_name()
            model_id = model.get_relation_model_id(target_field=target_field)

            if model_id is None:  # pragma: no cover
                continue

            field_name: Union[str, List, Tuple] = model.get_related_field_name(
                target_field=target_field
            )
            if isinstance(field_name, list):
                field_name = tuple(x for x in field_name)
            children = self.already_extracted.get(target_model).get(field_name)
            models = self.already_extracted.get(target_model).pk_models
            set_children_on_model(
                model=model,
                related=related,
                children=children,
                model_id=model_id,
                models=models,
                orders_by=orders_by.get(related, {}),
            )

        return model

    async def _prefetch_related_models(
        self, models: Sequence["Model"], rows: List
    ) -> Sequence["Model"]:
        """
        Main method of the query.

        Translates select nad prefetch list into dictionaries to avoid querying the
        same related models multiple times.

        Keeps the list of already extracted models.

        Extracts the related models from the database and later populate all children
        on each of the parent models from list.

        :param models: list of parent models from main query
        :type models: List[Model]
        :param rows: raw response from sql query
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :return: list of models with prefetch children populated
        :rtype: List[Model]
        """
        self.already_extracted.get(self.model.get_name()).raw = rows
        select_dict = translate_list_to_dict(self._select_related)
        prefetch_dict = translate_list_to_dict(self._prefetch_related)
        target_model = self.model
        orders_by = self.order_dict
        for related in prefetch_dict.keys():
            await self._extract_related_models(
                related=related,
                target_model=target_model,
                prefetch_dict=prefetch_dict.get(related, {}),
                select_dict=select_dict.get(related, {}),
                excludable=self.excludable,
                orders_by=orders_by.get(related, {}),
            )
        final_models = []
        for model in models:
            final_models.append(
                self._populate_nested_related(
                    model=model, prefetch_dict=prefetch_dict, orders_by=self.order_dict
                )
            )
        return models

    async def _extract_related_models(  # noqa: CFQ002, CCR001
        self,
        related: str,
        target_model: Type["Model"],
        prefetch_dict: Dict,
        select_dict: Dict,
        excludable: "ExcludableItems",
        orders_by: Dict,
    ) -> None:
        """
        Constructs queries with required ids and extracts data with fields that should
        be included/excluded.

        Runs the queries against the database and populated dictionaries with ids and
        with actual extracted children models.

        Calls itself recurrently to extract deeper nested relations of related model.

        :param related: name of the relation
        :type related: str
        :param target_model: model to which relation leads to
        :type target_model: Type[Model]
        :param prefetch_dict: prefetch related list converted into dictionary
        :type prefetch_dict: Dict
        :param select_dict: select related list converted into dictionary
        :type select_dict: Dict
        :param fields: fields to include
        :type fields: Union[Set[Any], Dict[Any, Any], None]
        :param exclude_fields: fields to exclude
        :type exclude_fields: Union[Set[Any], Dict[Any, Any], None]
        :param orders_by: dictionary of order bys clauses
        :type orders_by: Dict
        :return: None
        :rtype: None
        """
        target_field = target_model.Meta.model_fields[related]
        target_field = cast("ForeignKeyField", target_field)
        reverse = False
        if target_field.virtual or target_field.is_multi:
            reverse = True

        parent_model = target_model

        filter_clauses = self._get_filter_for_prefetch(
            parent_model=parent_model,
            target_model=target_field.to,
            reverse=reverse,
            related=related,
        )
        if not filter_clauses:  # related field is empty
            return

        already_loaded = select_dict is Ellipsis or related in select_dict

        if not already_loaded:
            # If not already loaded with select_related
            related_field_name = parent_model.get_related_field_name(
                target_field=target_field
            )
            table_prefix, exclude_prefix, rows = await self._run_prefetch_query(
                target_field=target_field,
                excludable=excludable,
                filter_clauses=filter_clauses,
                related_field_name=related_field_name,
            )
        else:
            rows = []
            table_prefix = ""
            exclude_prefix = ""

        if prefetch_dict and prefetch_dict is not Ellipsis:
            for subrelated in prefetch_dict.keys():
                await self._extract_related_models(
                    related=subrelated,
                    target_model=target_field.to,
                    prefetch_dict=prefetch_dict.get(subrelated, {}),
                    select_dict=self._get_select_related_if_apply(
                        subrelated, select_dict
                    ),
                    excludable=excludable,
                    orders_by=self._get_select_related_if_apply(subrelated, orders_by),
                )

        if not already_loaded:
            self._populate_rows(
                rows=rows,
                parent_model=parent_model,
                target_field=target_field,
                table_prefix=table_prefix,
                exclude_prefix=exclude_prefix,
                excludable=excludable,
                prefetch_dict=prefetch_dict,
                orders_by=orders_by,
            )
        else:
            self._update_already_loaded_rows(
                target_field=target_field,
                prefetch_dict=prefetch_dict,
                orders_by=orders_by,
            )

    async def _run_prefetch_query(
        self,
        target_field: "ForeignKeyField",
        excludable: "ExcludableItems",
        filter_clauses: List,
        related_field_name: Union[str, List[str]],
    ) -> Tuple[str, str, List]:
        """
        Actually runs the queries against the database and populates the raw response
        for given related model.

        Returns table prefix as it's later needed to eventually initialize the children
        models.

        :param target_field: ormar field with relation definition
        :type target_field: "BaseField"
        :param filter_clauses: list of clauses, actually one clause with ids of relation
        :type filter_clauses: List[sqlalchemy.sql.elements.TextClause]
        :return: table prefix and raw rows from sql response
        :rtype: Tuple[str, List]
        """
        if not isinstance(related_field_name, list):
            related_field_name = [related_field_name]
        target_model = target_field.to
        if (
            target_field.self_reference
            and target_field.self_reference_primary == target_field.name
        ):
            target_name = target_field.default_source_field_name()
        else:
            target_name = target_field.default_target_field_name()

        select_related = []
        query_target = target_model
        table_prefix = ""
        exclude_prefix = target_field.to.Meta.alias_manager.resolve_relation_alias(
            from_model=target_field.owner, relation_name=target_field.name
        )
        if target_field.is_multi:
            query_target = target_field.through
            select_related = [target_name]
            table_prefix = target_field.to.Meta.alias_manager.resolve_relation_alias(
                from_model=query_target, relation_name=target_name
            )
            exclude_prefix = table_prefix
            self.already_extracted.get(target_name).prefix = table_prefix

        model_excludable = excludable.get(model_cls=target_model, alias=exclude_prefix)
        # includes nested pks if not included already
        for related_name in related_field_name:
            if model_excludable.include and not model_excludable.is_included(
                related_name
            ):
                model_excludable.set_values({related_name}, is_exclude=False)

        qry = Query(
            model_cls=query_target,
            select_related=select_related,
            filter_clauses=filter_clauses,
            exclude_clauses=[],
            offset=None,
            limit_count=None,
            excludable=excludable,
            order_bys=None,
            limit_raw_sql=False,
        )
        expr = qry.build_select_expression()
        # print(expr.compile(compile_kwargs={"literal_binds": True}))
        rows = await self.database.fetch_all(expr)
        self.already_extracted.get(target_name).raw = rows
        return table_prefix, exclude_prefix, rows

    @staticmethod
    def _get_select_related_if_apply(related: str, select_dict: Dict) -> Dict:
        """
        Extract nested related of select_related dictionary to extract models nested
        deeper on related model and already loaded in select related query.

        :param related: name of the relation
        :type related: str
        :param select_dict: dictionary of select related models in main query
        :type select_dict: Dict
        :return: dictionary with nested related of select related
        :rtype: Dict
        """
        return (
            select_dict.get(related, {})
            if (select_dict and select_dict is not Ellipsis and related in select_dict)
            else {}
        )

    def _update_already_loaded_rows(  # noqa: CFQ002
        self, target_field: "BaseField", prefetch_dict: Dict, orders_by: Dict,
    ) -> None:
        """
        Updates models that are already loaded, usually children of children.

        :param target_field: ormar field with relation definition
        :type target_field: "BaseField"
        :param prefetch_dict: dictionaries of related models to prefetch
        :type prefetch_dict: Dict
        :param orders_by: dictionary of order by clauses by model
        :type orders_by: Dict
        """
        target_model = target_field.to
        for instance in self.models.get(target_model.get_name(), []):
            self._populate_nested_related(
                model=instance, prefetch_dict=prefetch_dict, orders_by=orders_by
            )

    def _populate_rows(  # noqa: CFQ002
        self,
        rows: List,
        target_field: "ForeignKeyField",
        parent_model: Type["Model"],
        table_prefix: str,
        exclude_prefix: str,
        excludable: "ExcludableItems",
        prefetch_dict: Dict,
        orders_by: Dict,
    ) -> None:
        """
        Instantiates children models extracted from given relation.

        Populates them with their own nested children if they are included in prefetch
        query.

        Sets the initialized models and ids of them under corresponding keys in
        already_extracted dictionary. Later those instances will be fetched by ids
        and set on the parent model after sorting if needed.

        :param excludable: structure of fields to include and exclude
        :type excludable: ExcludableItems
        :param rows: raw sql response from the prefetch query
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :param target_field: field with relation definition from parent model
        :type target_field: "BaseField"
        :param parent_model: model with relation definition
        :type parent_model: Type[Model]
        :param table_prefix: prefix of the target table from current relation
        :type table_prefix: str
        :param prefetch_dict: dictionaries of related models to prefetch
        :type prefetch_dict: Dict
        :param orders_by: dictionary of order by clauses by model
        :type orders_by: Dict
        """
        target_model = target_field.to
        for row in rows:
            field_name: Union[str, List, Tuple] = parent_model.get_related_field_name(
                target_field=target_field
            )
            item = target_model.extract_prefixed_table_columns(
                item={}, row=row, table_prefix=table_prefix, excludable=excludable,
            )
            item["__excluded__"] = target_model.get_names_to_exclude(
                excludable=excludable, alias=exclude_prefix
            )
            instance = target_model(**item)
            instance = self._populate_nested_related(
                model=instance, prefetch_dict=prefetch_dict, orders_by=orders_by
            )
            models = self.already_extracted.get(target_model.get_name()).pk_models
            pk_to_check = instance.pk
            if isinstance(pk_to_check, dict):
                pk_to_check = tuple((k, v) for k, v in pk_to_check.items())
            if pk_to_check not in models:
                models[pk_to_check] = instance

            if target_field.is_compound and len(target_field.names) > 1:
                if target_field.is_multi:
                    related_field = target_field.through.Meta.model_fields[
                        cast(str, field_name)
                    ]
                    names = related_field.names
                elif target_field.virtual:
                    related_field = target_model.Meta.model_fields[
                        cast(str, field_name)
                    ]
                    names = related_field.names
                else:
                    related_field = target_field
                    names = {x: x for x in related_field.names.keys()}
                    field_name = tuple(x for x in field_name)
                key = tuple(
                    sorted(
                        (own_name, row[target_name])
                        for own_name, target_name in names.items()
                    )
                )
            else:
                field_db_name = target_model.get_column_alias(cast(str, field_name))
                key = row[field_db_name]
            self.already_extracted.get(target_model.get_name()).get(
                field_name
            ).setdefault(key, UniqueList()).append(pk_to_check)
