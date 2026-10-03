"""Minimal agent registry for REST clients."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.db import get_session
from meldx.app.models import Agent, AgentUpsert

router = APIRouter(prefix="/agents", tags=["agents"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_agents(session: Session) -> list[Agent]:
    return list((await session.exec(select(Agent).order_by(col(Agent.id)))).all())


@router.post("")
async def upsert_agent(payload: AgentUpsert, session: Session) -> Agent:
    agent = await session.get(Agent, payload.id)
    if agent is None:
        agent = Agent(**payload.model_dump())
    else:
        agent.type = payload.type
        agent.status = payload.status
    agent.last_seen = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent
