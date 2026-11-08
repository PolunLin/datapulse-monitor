from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.record import Record
from app.schemas.record import RecordFilters


async def get_by_id(
    session: AsyncSession, record_id: int, *, for_update: bool = False
) -> Record | None:
    statement = select(Record).where(Record.id == record_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def add(session: AsyncSession, record: Record) -> Record:
    session.add(record)
    await session.flush()
    return record


async def list_page(session: AsyncSession, filters: RecordFilters) -> tuple[list[Record], int]:
    conditions = []
    if filters.category is not None:
        conditions.append(Record.category == filters.category)
    if filters.q is not None:
        conditions.append(Record.title.icontains(filters.q, autoescape=True))
    if filters.created_by is not None:
        conditions.append(Record.created_by == filters.created_by)
    if filters.start_time is not None:
        conditions.append(Record.timestamp >= filters.start_time)
    if filters.end_time is not None:
        conditions.append(Record.timestamp < filters.end_time)

    total = await session.scalar(select(func.count(Record.id)).where(*conditions))
    sort_columns = {
        "timestamp": Record.timestamp,
        "value": Record.value,
        "title": Record.title,
        "id": Record.id,
    }
    sort_column = sort_columns[filters.sort_by]
    order = sort_column.asc() if filters.sort_order == "asc" else sort_column.desc()
    tie_break = Record.id.asc() if filters.sort_order == "asc" else Record.id.desc()
    ordering = [order] if filters.sort_by == "id" else [order, tie_break]
    statement = (
        select(Record)
        .where(*conditions)
        .order_by(*ordering)
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    result = await session.scalars(statement)
    return list(result), total or 0


async def delete(session: AsyncSession, record: Record) -> None:
    await session.delete(record)
