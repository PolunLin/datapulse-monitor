from app.core.exceptions import NotFoundError
from app.db.base import utc_now
from app.db.session import SessionFactory
from app.models.record import Record
from app.repositories import records
from app.schemas.record import RecordCreate, RecordFilters, RecordPage, RecordRead, RecordUpdate
from app.services.auth import Principal
from app.services.authorization import require_record_owner_or_admin, require_record_writer


class RecordService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def create(self, principal: Principal, payload: RecordCreate) -> RecordRead:
        require_record_writer(principal)
        async with self.session_factory.begin() as session:
            record = await records.add(
                session, Record(**payload.model_dump(), created_by=principal.id)
            )
            response = RecordRead.model_validate(record)
        return response

    async def get(self, record_id: int) -> RecordRead:
        async with self.session_factory() as session:
            record = await records.get_by_id(session, record_id)
            if record is None:
                raise NotFoundError("Record not found")
            return RecordRead.model_validate(record)

    async def list(self, filters: RecordFilters) -> RecordPage:
        async with self.session_factory() as session:
            items, total = await records.list_page(session, filters)
            return RecordPage(
                items=[RecordRead.model_validate(item) for item in items],
                total=total,
                page=filters.page,
                page_size=filters.page_size,
            )

    async def update(
        self, principal: Principal, record_id: int, payload: RecordUpdate
    ) -> RecordRead:
        require_record_writer(principal)
        async with self.session_factory.begin() as session:
            record = await records.get_by_id(session, record_id, for_update=True)
            if record is None:
                raise NotFoundError("Record not found")
            require_record_owner_or_admin(principal, record.created_by)
            for name, value in payload.model_dump(exclude_unset=True).items():
                setattr(record, name, value)
            record.updated_at = utc_now()
            await session.flush()
            response = RecordRead.model_validate(record)
        return response

    async def delete(self, principal: Principal, record_id: int) -> None:
        require_record_writer(principal)
        async with self.session_factory.begin() as session:
            record = await records.get_by_id(session, record_id, for_update=True)
            if record is None:
                raise NotFoundError("Record not found")
            require_record_owner_or_admin(principal, record.created_by)
            await records.delete(session, record)
