# PHASE 8 REVIEW PACKET

Evidence-only collection from the current canonical workspace. Five targets are
included. No tests, compilation, services, Sol, or Git diff were invoked while
collecting this packet.

## Target A

### File

`src/dovideo/infrastructure/persistence/repository.py`

### Symbols

`_stage_value`, `_durable_upsert`, `_durable_upsert_many`, `_durable_delete`,
`CheckpointRepository.write`, `write_stage`,
`_cache_write_field`, `_cache_write_stage`, `_cache_register`, `_cache_evict`.

### Source

```python
def _stage_value(stage: TaskStage | str | None) -> str:
    if isinstance(stage, TaskStage):
        return stage.value
    return "" if stage is None else str(stage)

    def _durable_upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None,
        payload: str | None,
    ) -> None:
        try:
            writer = getattr(self.durable, "upsert", None)
            if not callable(writer):
                raise TypeError("durable adapter has no upsert operation")
            writer(media_id, checkpoint_name, _stage_value(stage), payload)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointWriteError("保存 Agent Checkpoint 失败") from exc

    def _durable_upsert_many(self, records: Sequence[CheckpointRecord]) -> None:
        """Use an adapter transaction when available, otherwise preserve order."""

        try:
            writer_many = getattr(self.durable, "upsert_many", None)
            if callable(writer_many):
                writer_many(records)
                return
            for item in records:
                self._durable_upsert(
                    item.media_id,
                    item.checkpoint_name,
                    item.stage,
                    item.payload,
                )
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointWriteError("保存 Agent Checkpoint 失败") from exc

    def _durable_delete(self, media_id: int, checkpoint_name: str) -> None:
        try:
            deleter = getattr(self.durable, "delete", None)
            if not callable(deleter):
                raise TypeError("durable adapter has no delete operation")
            deleter(media_id, checkpoint_name)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc
```

```python
    def write(
        self,
        media_id: int,
        checkpoint_name: str,
        stage_checkpoint_name: str,
        redis_key: str,
        field: str,
        stage: TaskStage | str,
        value: Any,
    ) -> None:
        """Persist payload and stage rows, then warm both cache fields."""

        payload = self.codec.encode(value)
        stage_text = _stage_value(stage)
        self._durable_upsert_many(
            (
                CheckpointRecord(media_id, checkpoint_name, stage_text, payload),
                CheckpointRecord(media_id, stage_checkpoint_name, stage_text, None),
            )
        )
        self._cache_write_field(
            redis_key,
            field,
            payload,
            stage_text,
            media_id=media_id,
            checkpoint_name=checkpoint_name,
        )

    def write_stage(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
        stage: TaskStage | str,
    ) -> None:
        stage_text = _stage_value(stage)
        # This deliberately completes durable upsert before touching cache.
        self._durable_upsert(media_id, checkpoint_name, stage_text, None)
        self._cache_write_stage(
            redis_key,
            stage_text,
            media_id=media_id,
            checkpoint_name=checkpoint_name,
        )
```

```python
    def _cache_evict(self, redis_key: str, *fields: str) -> None:
        try:
            self._cache_delete_fields(redis_key, *fields)
        except Exception:
            pass

    def _cache_write_field(
        self,
        redis_key: str,
        field: str,
        payload: str | None,
        stage: str | None,
        *,
        media_id: int | None = None,
        checkpoint_name: str | None = None,
    ) -> None:
        if self.cache is None or payload is None:
            if self.cache is not None and stage:
                self._cache_write_stage(
                    redis_key,
                    stage,
                    media_id=media_id,
                    checkpoint_name=checkpoint_name,
                )
            return
        fields = [field]
        if stage:
            fields.append("stage")
        try:
            self._cache_set(redis_key, field, payload)
            if stage:
                self._cache_set(redis_key, "stage", stage)
            self._cache_expire(redis_key)
            self._cache_register(media_id, checkpoint_name, redis_key)
        except Exception:
            self._cache_evict(redis_key, *fields)

    def _cache_write_stage(
        self,
        redis_key: str,
        stage: str,
        *,
        media_id: int | None = None,
        checkpoint_name: str | None = None,
    ) -> None:
        if self.cache is None:
            return
        try:
            self._cache_set(redis_key, "stage", stage)
            self._cache_expire(redis_key)
            self._cache_register(media_id, checkpoint_name, redis_key)
        except Exception:
            self._cache_evict(redis_key, "stage")

    def _cache_register(
        self,
        media_id: int | None,
        checkpoint_name: str | None,
        redis_key: str,
    ) -> None:
        if self.cache is None or media_id is None:
            return
        method = getattr(self.cache, "register_checkpoint_key", None)
        if callable(method):
            method(media_id, checkpoint_name or "", redis_key)
            return
        method = getattr(self.cache, "register_media_key", None)
        if callable(method):
            method(media_id, redis_key)
```

### Relevant Tests

- `tests/infrastructure/test_checkpoint_repository_8a.py::test_durable_failure_is_typed_and_cache_is_not_touched`
- `tests/infrastructure/test_checkpoint_repository_8a.py::test_durable_commit_happens_before_cache_write`
- `tests/infrastructure/test_checkpoint_repository_8a.py::test_cache_read_and_write_outage_does_not_break_durable_flow`
- `tests/infrastructure/test_checkpoint_integration_8e.py::test_durable_rollback_keeps_last_valid_checkpoint_and_cache_value`

### Luna Note

These excerpts cover the durable two-record write, typed durable exception boundaries, cache publication after durable completion, and cache-field eviction on update failure.

## Target B

### File

`src/dovideo/infrastructure/persistence/codec.py`

### Symbols

`CheckpointVersion`, `_jsonable`, `JsonCheckpointCodec.__init__`, `encode`,
`decode`, and the serialization/deserialization aliases.

### Source

```python
@dataclass(frozen=True, slots=True)
class CheckpointVersion:
    """Version tuple that prevents old payloads being silently reused."""

    schema_version: int | str = 1
    prompt_version: str = "v1"
    embedding_version: str = "v1"

    def as_json(self) -> dict[str, int | str]:
        return {
            "schemaVersion": self.schema_version,
            "promptVersion": self.prompt_version,
            "embeddingVersion": self.embedding_version,
        }


def _jsonable(value: Any) -> Any:
    """Convert common domain/container values to JSON-compatible objects."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return value
```

```python
class JsonCheckpointCodec:
    def __init__(
        self,
        schema_version: int | str | CheckpointVersion = 1,
        prompt_version: str = "v1",
        embedding_version: str = "v1",
        *,
        version: CheckpointVersion | int | str | None = None,
        namespace_version: int | str | None = None,
    ) -> None:
        if isinstance(schema_version, CheckpointVersion):
            resolved = schema_version
        elif isinstance(version, CheckpointVersion):
            resolved = version
        else:
            resolved_schema = (
                namespace_version
                if namespace_version is not None
                else version
                if version is not None
                else schema_version
            )
            resolved = CheckpointVersion(
                schema_version=resolved_schema,
                prompt_version=prompt_version,
                embedding_version=embedding_version,
            )
        self.version = resolved

    def encode(self, value: Any) -> str:
        document = self.version.as_json()
        document["payload"] = _jsonable(value)
        try:
            return json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except Exception as exc:  # pragma: no cover - exact json error varies
            raise CheckpointSerializationError(
                "保存 Agent Checkpoint JSON 失败"
            ) from exc

    def decode(self, payload: str | bytes | bytearray, target_type: Any = None) -> Any:
        try:
            document = json.loads(payload)
        except Exception as exc:
            raise CheckpointDeserializationError(
                "读取 Agent Checkpoint JSON 失败"
            ) from exc
        if not isinstance(document, dict):
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload must be a JSON object"
            )
        expected = self.version.as_json()
        if any(document.get(key) != value for key, value in expected.items()):
            raise CheckpointVersionMismatchError(
                "Agent Checkpoint version mismatch"
            )
        if "payload" not in document:
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload field is missing"
            )
        value = document["payload"]
        if target_type is None or target_type is Any or target_type is object:
            return value
        try:
            model_validate = getattr(target_type, "model_validate", None)
            if callable(model_validate):
                return model_validate(value)
            validate_python = getattr(target_type, "validate_python", None)
            if callable(validate_python):
                return validate_python(value)
            return TypeAdapter(target_type).validate_python(value)
        except Exception as exc:
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload model validation failed"
            ) from exc

    dumps = encode
    loads = decode
    serialize = encode
    deserialize = decode
    encode_value = encode
    decode_value = decode
```

### Relevant Tests

- `tests/infrastructure/test_checkpoint_repository_8a.py::test_alias_json_roundtrip_for_agent_and_video_models`
- `tests/infrastructure/test_checkpoint_repository_8a.py::test_version_mismatch_is_cache_miss_and_evicts_old_field`
- `tests/infrastructure/test_checkpoint_repository_8a.py::test_version_mismatch_in_durable_payload_is_typed`
- `tests/infrastructure/test_checkpoint_integration_8e.py::test_version_mismatch_is_not_reused_and_new_codec_can_replace_it`

### Luna Note

The source directly shows alias-aware JSON conversion, schema/prompt/embedding version validation, malformed-document handling, typed Pydantic/`TypeAdapter` deserialization, and no unsafe object deserialization.

## Target C

### File

`src/dovideo/application/checkpoint_service.py`,
`src/dovideo/application/value_objects.py`, and
`src/dovideo/application/analysis_task_keys.py`

### Symbols

`AgentCheckpointService._goal_coordinates`, `_stage_checkpoint`,
`load_context`, `save_context`, `load_chunks`, `save_chunks`, `load_plan`,
`save_plan`, `load_critic_state`, `save_execution_state`, `save_critic_state`,
`load_result`, `save_result`, `load_stage`, `save_stage`, `stage_revision`,
`begin_staged_revision`, `complete_staged_revision`, `save_failure`,
`delete_media`, `TaskKey`, `goal_digest`, `normalize_content_hash`, and
`AnalysisTaskKeys`.

### Source

```python
    @classmethod
    def _goal_coordinates(
        cls,
        key: TaskKey,
        field: str,
    ) -> tuple[int, str, str, str]:
        mode = key.mode
        return (
            key.media_id,
            cls.goal_checkpoint(key.goal, mode, field),
            cls.goal_key(key.media_id, key.goal, mode),
            field,
        )

    @classmethod
    def _stage_checkpoint(cls, key: TaskKey) -> str:
        return cls.goal_checkpoint(key.goal, key.mode, "stage")

    async def load_context(self, media_id: int) -> VideoContext | None:
        return await self._call(
            "read", media_id, self.media_checkpoint("context"),
            self.checkpoint_key(media_id), "context", VideoContext,
        )

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        reusable = VideoContext(source=context.source, user_goal="", segments=tuple(context.segments))
        await self._call(
            "write", media_id, self.media_checkpoint("context"),
            self.media_checkpoint("stage"), self.checkpoint_key(media_id),
            "context", TaskStage.CONTEXT_COMPLETED, reusable,
        )

    async def load_chunks(self, media_id: int) -> tuple[VideoChunk, ...] | None:
        chunks = await self._call(
            "read", media_id, self.media_checkpoint("chunks"),
            self.checkpoint_key(media_id), "chunks", tuple[VideoChunk, ...],
        )
        return None if chunks is None else tuple(chunks)

    async def save_chunks(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        copied = tuple(chunks)
        await self._call(
            "write", media_id, self.media_checkpoint("chunks"),
            self.media_checkpoint("stage"), self.checkpoint_key(media_id),
            "chunks", TaskStage.CHUNKS_COMPLETED, copied,
        )
```

```python
    async def load_plan(self, key: TaskKey) -> AgentPlan | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "plan")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentPlan)

    async def save_plan(self, key: TaskKey, plan: AgentPlan) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "plan")
        await self._call(
            "write", media_id, checkpoint, self._stage_checkpoint(key),
            redis_key, field, TaskStage.PLAN_COMPLETED, plan,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_critic_state(self, key: TaskKey) -> AgentState | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentState)

    async def save_execution_state(self, key: TaskKey, state: AgentState) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        await self._call(
            "write", media_id, checkpoint, self._stage_checkpoint(key),
            redis_key, field, TaskStage.EXECUTOR_COMPLETED, state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def save_critic_state(self, key: TaskKey, state: AgentState) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        stage = (
            TaskStage.CRITIC_PASSED
            if state.critique is not None and state.critique.passed
            else TaskStage.CRITIC_RETRY_REQUIRED
        )
        await self._call(
            "write", media_id, checkpoint, self._stage_checkpoint(key),
            redis_key, field, stage, state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_result(self, key: TaskKey) -> AgentState | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "result")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentState)

    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "result")
        stage = (
            TaskStage.ANALYSIS_COMPLETED
            if state.critique is not None and state.critique.passed
            else TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS
        )
        await self._call(
            "write", media_id, checkpoint, self._stage_checkpoint(key),
            redis_key, field, stage, state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        return await self._call(
            "read_stage", key.media_id, self._stage_checkpoint(key),
            self.goal_key(key.media_id, key.goal, key.mode),
        )

    async def save_stage(self, key: TaskKey, stage: TaskStage) -> None:
        await self._call(
            "write_stage", key.media_id, self._stage_checkpoint(key),
            self.goal_key(key.media_id, key.goal, key.mode), stage,
        )
        await self._remember_goal_key(
            key.media_id, self.goal_key(key.media_id, key.goal, key.mode)
        )
```

```python
    async def stage_revision(
        self,
        media_id: int,
        goal: str,
        plan: AgentPlan | Any | None = None,
        mode: AnalysisMode | str | None = None,
    ) -> None:
        if isinstance(plan, (AnalysisMode, str)) and (
            mode is None or isinstance(mode, (AgentPlan, dict))
        ):
            plan, mode = mode, plan
        resolved_plan = self._coerce_revision_plan(plan)
        revision_checkpoint = self.revision_checkpoint(goal, mode)
        revision_key = self.revision_key(media_id, goal, mode)
        await self._call(
            "write_standalone", media_id, revision_checkpoint, revision_key,
            "revision", TaskStage.REVISION_PENDING,
            RevisionCheckpoint(plan=resolved_plan, applied=False),
        )
        await self._remember_goal_key(media_id, revision_key)

    async def begin_staged_revision(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> bool:
        revision_checkpoint = self.revision_checkpoint(goal, mode)
        revision_key = self.revision_key(media_id, goal, mode)
        revision = await self._call(
            "read", media_id, revision_checkpoint, revision_key,
            "revision", RevisionCheckpoint,
        )
        if revision is None:
            return False
        if not isinstance(revision, RevisionCheckpoint):
            revision = RevisionCheckpoint.model_validate(revision)
        if revision.applied:
            return True
        goal_key = self.goal_key(media_id, goal, mode)
        await self._call(
            "delete_prefix", media_id, self.goal_checkpoint(goal, mode, ""),
            redis_key=goal_key,
        )
        if revision.plan is not None:
            await self.save_plan(
                TaskKey(media_id, goal, AnalysisMode.from_nullable(mode)),
                revision.plan,
            )
        await self._call(
            "write_standalone", media_id, revision_checkpoint, revision_key,
            "revision", TaskStage.REVISION_APPLIED,
            RevisionCheckpoint(plan=revision.plan, applied=True),
        )
        await self._remember_goal_key(media_id, revision_key)
        return True

    async def complete_staged_revision(self, media_id: int, goal: str,
                                       mode: AnalysisMode | str | None = None) -> None:
        await self._call("delete", media_id, self.revision_checkpoint(goal, mode),
                         self.revision_key(media_id, goal, mode))

    async def cancel_staged_revision(self, media_id: int, goal: str,
                                     mode: AnalysisMode | str | None = None) -> None:
        await self.complete_staged_revision(media_id, goal, mode)
```

```python
    async def save_failure(
        self, media_id: int, goal: str, failed_stage: TaskStage | str,
        error: BaseException, mode: AnalysisMode | str | None = None,
    ) -> None:
        stage = (
            failed_stage if isinstance(failed_stage, TaskStage)
            else TaskStage.from_value(str(failed_stage))
        )
        if stage is None:
            raise ValueError(f"unknown failed stage: {failed_stage}")
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception")
        key = self.goal_key(media_id, goal, mode)
        await self._call(
            "write_stage", media_id, self.goal_checkpoint(goal, mode, "stage"),
            key, TaskStage.FAILED,
        )
        cache = self._cache_object()
        if cache is not None:
            try:
                await self._cache_call(("set_hash", "hset", "put"),
                                       key, "failedStage", stage.value)
                await self._cache_call(("set_hash", "hset", "put"),
                                       key, "errorType", error.__class__.__name__)
                await self._cache_call(("expire",), key, 7 * 24 * 60 * 60)
            except Exception:
                pass
        await self._remember_goal_key(media_id, key)

    async def delete_media(self, media_id: int) -> None:
        keys = [self.checkpoint_key(media_id), self.feedback_key(media_id),
                self.goal_index_key(media_id)]
        try:
            keys.extend(await self._cache_members(self.goal_index_key(media_id)))
        except Exception:
            pass
        await self._call("delete_media", media_id, tuple(dict.fromkeys(keys)))
```

```python
def goal_digest(goal: str, mode: AnalysisMode | str | None = None) -> str:
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("analysis goal is required")
    trimmed = goal.strip()
    name = _mode_name(mode)
    if name == AnalysisMode.GENERAL.name:
        return _sha256(trimmed)
    return _sha256(f"{name}\u241f{trimmed}")


def normalize_content_hash(media_id: int, content_hash: str | None) -> str:
    if isinstance(content_hash, str) and _MD5_PATTERN.fullmatch(content_hash):
        return content_hash.lower()
    return f"media-{media_id}"


class AnalysisTaskKeys:
    goal_digest = staticmethod(goal_digest)
    normalize_content_hash = staticmethod(normalize_content_hash)
    active = staticmethod(active)
    lock = staticmethod(lock)
    completed = staticmethod(completed)
    attempts = staticmethod(attempts)
    context_owner = staticmethod(context_owner)
    context_lock = staticmethod(context_lock)
    goalDigest = staticmethod(goal_digest)
    normalizeContentHash = staticmethod(normalize_content_hash)
    contextOwner = staticmethod(context_owner)
    contextLock = staticmethod(context_lock)
```

```python
@dataclass(frozen=True, slots=True)
class TaskKey:
    media_id: int
    goal: str
    mode: AnalysisMode = AnalysisMode.GENERAL

    def __post_init__(self) -> None:
        if isinstance(self.media_id, bool) or not isinstance(self.media_id, int):
            raise TypeError("media_id must be an integer")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("analysis goal is required")
        object.__setattr__(self, "goal", self.goal.strip())
        object.__setattr__(self, "mode", _resolve_mode(self.mode))
```

### Relevant Tests

- `tests/application/test_checkpoint_service_8b.py::test_context_is_media_scoped_and_saved_goal_is_empty`
- `tests/application/test_checkpoint_service_8b.py::test_goal_plan_critic_result_and_stage_methods_use_exact_names`
- `tests/application/test_checkpoint_service_8c.py::test_staged_revision_applies_once_and_is_idempotent`
- `tests/application/test_checkpoint_service_8c.py::test_failure_stage_is_durable_and_cache_metadata_is_best_effort`
- `tests/infrastructure/test_checkpoint_integration_8e.py::test_loop_goal_and_mode_namespaces_isolate_results_while_context_reuses`

### Luna Note

The excerpts show media-scoped context/chunks and goal+mode-scoped analysis
state, plus revision, failed-stage, and deletion paths. `TaskKey` normalizes
the request identity before the digest helpers construct Java-shaped keys.

## Target D

### File

`src/dovideo/infrastructure/persistence/mysql_checkpoint.py`

### Symbols

`MySqlCheckpointStore.__init__`, `_execute`, `read`, `upsert`, `upsert_many`,
`delete`, `delete_prefix`, and `delete_media`.

### Source

```python
class MySqlCheckpointStore:
    def __init__(
        self,
        connection_factory: ConnectionFactory | None = None,
        *,
        factory: ConnectionFactory | None = None,
        connect: ConnectionFactory | None = None,
        connection: Any | None = None,
        client: Any | None = None,
    ) -> None:
        candidates = [value for value in (connection_factory, factory, connect) if value is not None]
        if len(candidates) > 1:
            raise ValueError("only one MySQL connection factory may be supplied")
        if connection is not None and client is not None:
            raise ValueError("connection and client are mutually exclusive")
        injected = connection if connection is not None else client
        if candidates and injected is not None:
            raise ValueError("connection factory and connection are mutually exclusive")
        if candidates:
            resolved = candidates[0]
            if not callable(resolved):
                raise TypeError("MySQL connection factory must be callable")
            self._connection_factory = resolved
            self._owns_connection = True
            self._connection = None
        elif injected is not None:
            if callable(injected) and not hasattr(injected, "cursor"):
                self._connection_factory = injected
                self._owns_connection = True
                self._connection = None
            else:
                if not hasattr(injected, "cursor"):
                    raise TypeError("MySQL connection must expose cursor()")
                self._connection_factory = lambda: injected
                self._owns_connection = False
                self._connection = injected
        else:
            raise ValueError("a MySQL connection factory or connection is required")
        self._lock = threading.RLock()

    def _execute(self, operation: Callable[[Any], T], *, write: bool) -> T:
        with self._lock:
            connection = None
            cursor = None
            try:
                connection = self._connection_factory()
                cursor = connection.cursor()
                result = operation(cursor)
                if write:
                    connection.commit()
                return result
            except CheckpointError:
                raise
            except Exception as exc:
                if write and connection is not None:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                error_type = CheckpointWriteError if write else CheckpointReadError
                if operation.__name__.startswith("delete"):
                    error_type = CheckpointDeleteError
                raise error_type("MySQL Agent Checkpoint operation failed") from exc
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if self._owns_connection and connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
```

```python
    def read(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        def read_one(cursor: Any) -> CheckpointRecord | None:
            cursor.execute(
                """
                SELECT media_id, checkpoint_key, stage, payload, updated_at
                FROM agent_checkpoints
                WHERE media_id = %s AND checkpoint_key = %s
                """,
                (int(media_id), str(checkpoint_name)),
            )
            row = cursor.fetchone()
            return None if row is None else _record(row)

        return self._execute(read_one, write=False)

    def upsert(self, media_id: int, checkpoint_name: str,
               stage: TaskStage | str | None, payload: str | None) -> None:
        def put(cursor: Any) -> None:
            cursor.execute(
                """
                INSERT INTO agent_checkpoints
                    (media_id, checkpoint_key, stage, payload)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    stage = VALUES(stage), payload = VALUES(payload),
                    updated_at = CURRENT_TIMESTAMP(3)
                """,
                (int(media_id), str(checkpoint_name), _stage_value(stage), payload),
            )
        self._execute(put, write=True)

    def upsert_many(self, records: Iterable[CheckpointRecord]) -> None:
        values = tuple(records)

        def put_many(cursor: Any) -> None:
            statement = """
                INSERT INTO agent_checkpoints
                    (media_id, checkpoint_key, stage, payload)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    stage = VALUES(stage), payload = VALUES(payload),
                    updated_at = CURRENT_TIMESTAMP(3)
            """
            for item in values:
                cursor.execute(
                    statement,
                    (int(item.media_id), str(item.checkpoint_name),
                     _stage_value(item.stage), item.payload),
                )
        self._execute(put_many, write=True)
```

```python
    def delete(self, media_id: int, checkpoint_name: str) -> None:
        def delete_one(cursor: Any) -> None:
            cursor.execute(
                "DELETE FROM agent_checkpoints WHERE media_id = %s AND checkpoint_key = %s",
                (int(media_id), str(checkpoint_name)),
            )
        self._execute(delete_one, write=True)

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        def delete_prefix_rows(cursor: Any) -> None:
            cursor.execute(
                """
                DELETE FROM agent_checkpoints
                WHERE media_id = %s
                  AND checkpoint_key LIKE CONCAT(%s, '%')
                """,
                (int(media_id), str(checkpoint_prefix)),
            )
        self._execute(delete_prefix_rows, write=True)

    def delete_media(self, media_id: int) -> None:
        def delete_media_rows(cursor: Any) -> None:
            cursor.execute(
                "DELETE FROM agent_checkpoints WHERE media_id = %s",
                (int(media_id),),
            )
        self._execute(delete_media_rows, write=True)
```

### Relevant Tests

- `tests/infrastructure/test_persistence_8d.py::test_mysql_checkpoint_store_uses_parameterized_sql_and_commits`
- `tests/infrastructure/test_persistence_8d.py::test_mysql_checkpoint_store_rolls_back_and_closes_on_failure`
- `tests/infrastructure/test_persistence_8d.py::test_mysql_checkpoint_store_reads_and_deletes`

### Luna Note

The source shows injected connection ownership, parameterized `%s` SQL,
commit/rollback selection, cursor close, owned-connection close, and typed
error conversion. It exposes no credentials or connection representation in
the public error message.

## Target E

### File

`tests/infrastructure/test_checkpoint_integration_8e.py`

### Symbols

`_context`, `_plan`, `_result`, `_ContextRole`, `_PlannerRole`,
`_ExecutorRole`, `_CriticRole`, `_stack`, and the complete
`test_failed_critic_checkpoint_restarts_next_round_with_targeted_refresh_and_replan`.

### Source

```python
def _context(goal: str = "goal", transcript: str = "claim") -> VideoContext:
    return VideoContext(
        source="video.mp4", userGoal=goal,
        segments=(VideoSegment(startMs=0, endMs=60_000, transcript=transcript),),
    )


def _plan(task: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=(task,))


def _result(claim: str = "claim") -> AnalysisResult:
    return AnalysisResult(
        title="title", conclusions=(claim,),
        evidence=(AnalysisEvidence(timestampMs=1_000, source="ASR",
                                   content="claim", claim=claim),),
    )


class _ContextRole:
    def __init__(self, *, fail_refine: bool = False) -> None:
        self.select_calls = 0
        self.refine_calls = 0
        self.fail_refine = fail_refine

    async def select_relevant(self, context: VideoContext,
                              media_id: int | None = None) -> VideoContext:
        del media_id
        self.select_calls += 1
        return context

    async def refine_for_critique(self, media_id: int | None,
                                  full_context: VideoContext,
                                  selected_context: VideoContext,
                                  critique: CriticResult) -> VideoContext:
        del media_id, critique
        self.refine_calls += 1
        if self.fail_refine:
            self.fail_refine = False
            raise RuntimeError("simulated process interruption")
        return selected_context or full_context


class _PlannerRole:
    def __init__(self, *, planned: AgentPlan | None = None,
                 replanned: AgentPlan | None = None) -> None:
        self.planned = planned or _plan()
        self.replanned = replanned or _plan("replanned")
        self.plan_calls = 0
        self.replan_calls = 0

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del context, instruction
        self.plan_calls += 1
        return self.planned

    async def replan(self, context: VideoContext, current_plan: AgentPlan,
                     critique: CriticResult, *, instruction: str = "") -> AgentPlan:
        del context, current_plan, critique, instruction
        self.replan_calls += 1
        return self.replanned


class _ExecutorRole:
    def __init__(self, *, result: AnalysisResult | None = None,
                 failure: Exception | None = None) -> None:
        self.result = result or _result()
        self.failure = failure
        self.calls = 0

    async def execute(self, context: VideoContext, plan: AgentPlan,
                      previous_critique: CriticResult | None = None,
                      *, instruction: str = "") -> AnalysisResult:
        del context, plan, previous_critique, instruction
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class _CriticRole:
    def __init__(self, *critiques: CriticResult | None) -> None:
        self.critiques = list(critiques) or [CriticResult(passed=True)]
        self.calls = 0

    async def critique(self, context: VideoContext, plan: AgentPlan,
                       result: AnalysisResult | None, *,
                       instruction: str = "") -> CriticResult | None:
        del context, plan, result, instruction
        self.calls += 1
        if len(self.critiques) > 1:
            return self.critiques.pop(0)
        return self.critiques[0]
```

```python
def _stack(path: Path, *, context: _ContextRole | None = None,
           planner: _PlannerRole | None = None,
           executor: _ExecutorRole | None = None,
           critic: _CriticRole | None = None,
           max_rounds: int = 2):
    db = SqliteCheckpointStore(path)
    checkpoint = AgentCheckpointService(
        CheckpointRepository(db, InMemoryHotCheckpointCache(), JsonCheckpointCodec())
    )
    context = context or _ContextRole()
    planner = planner or _PlannerRole()
    executor = executor or _ExecutorRole()
    critic = critic or _CriticRole()
    telemetry = _Telemetry()
    loop = AgentLoopService(
        context, planner, executor, checkpoint, None, telemetry, critic,
        budget_config={"maxRounds": max_rounds},
    )
    return loop, checkpoint, db, context, planner, executor, critic, telemetry


@pytest.mark.asyncio
async def test_failed_critic_checkpoint_restarts_next_round_with_targeted_refresh_and_replan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "critic-failure.sqlite3"
    failed = CriticResult(
        passed=False, requiredTimestamps=(30_000,),
        missingRequirements=("quiz",),
    )
    first_context = _ContextRole(fail_refine=True)
    first = _stack(path, context=first_context,
                   critic=_CriticRole(failed), max_rounds=2)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        await first[0].run(_context(), media_id=7)
    assert first[5].calls == 1
    assert first[6].calls == 1
    first[2].close()

    second_planner = _PlannerRole(replanned=_plan("quiz"))
    second_context = _ContextRole()
    second = _stack(path, context=second_context,
                    planner=second_planner, max_rounds=2)
    returned = await second[0].run(_context(), media_id=7)
    assert returned.round == 2
    assert returned.critique is not None and returned.critique.passed
    assert second_planner.plan_calls == 0
    assert second_planner.replan_calls == 1
    assert second_context.refine_calls == 1
    assert second[5].calls == 1
    assert second[6].calls == 1
    assert second[7].counts["criticEvidenceRefreshes"] == 1
    assert second[7].counts["planRevisions"] == 1
    second[2].close()
```

### Relevant Tests

- `tests/infrastructure/test_checkpoint_integration_8e.py::test_failed_critic_checkpoint_restarts_next_round_with_targeted_refresh_and_replan`
- `tests/application/test_agent_loop_7e.py::test_two_round_failure_refreshes_context_and_stops_on_second_pass`
- `tests/application/test_agent_loop_7e.py::test_missing_requirement_replans_successfully_and_persists_new_plan`
- `tests/application/test_agent_loop_7e.py::test_draft_checkpoint_resumes_at_critic_without_executor`

### Luna Note

The test uses real `SqliteCheckpointStore` and `AgentCheckpointService`; fake
roles only expose call counts and inject one refinement interruption. The
second run reads the failed Critic checkpoint, refreshes evidence, replans
once, and advances to round two.
