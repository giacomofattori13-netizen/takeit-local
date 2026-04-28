from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.models import MenuItem
from app.schemas import MenuItemCreate, MenuItemRead, MenuItemUpdate
from app.security import require_admin_api_key

router = APIRouter(
    prefix="/menu",
    tags=["menu"],
    dependencies=[Depends(require_admin_api_key)],
)

SessionDep = Annotated[Session, Depends(get_session)]


def normalize_menu_name(name: str) -> str:
    return " ".join(name.strip().split())


def find_duplicate_menu_item(
    session: Session,
    name: str,
    pizza_type: str,
    exclude_id: int | None = None,
) -> MenuItem | None:
    normalized_name = normalize_menu_name(name)

    statement = select(MenuItem).where(
        func.lower(MenuItem.name) == normalized_name.lower(),
        func.lower(MenuItem.pizza_type) == pizza_type.lower(),
    )

    items = session.exec(statement).all()

    for item in items:
        if exclude_id is not None and item.id == exclude_id:
            continue
        return item

    return None


@router.post("/", response_model=MenuItemRead)
def create_menu_item(menu_item: MenuItemCreate, session: SessionDep):
    normalized_name = normalize_menu_name(menu_item.name)

    duplicate = find_duplicate_menu_item(
        session=session,
        name=normalized_name,
        pizza_type=menu_item.pizza_type,
    )

    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"Menu item already exists: {duplicate.name} ({duplicate.pizza_type})",
        )

    db_item = MenuItem(
        name=normalized_name,
        category=menu_item.category.strip(),
        pizza_type=menu_item.pizza_type.strip(),
        price=menu_item.price,
        available=menu_item.available,
    )

    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    return db_item


@router.get("/", response_model=list[MenuItemRead])
def list_menu_items(session: SessionDep):
    statement = select(MenuItem)
    items = session.exec(statement).all()
    return items


@router.get("/search")
def search_menu_item(
    name: str,
    pizza_type: str,
    session: SessionDep,
):
    normalized_name = normalize_menu_name(name)

    statement = select(MenuItem).where(
        func.lower(MenuItem.name) == normalized_name.lower(),
        func.lower(MenuItem.pizza_type) == pizza_type.lower(),
    )

    item = session.exec(statement).first()

    if not item:
        return {"found": False}

    return {
        "found": True,
        "id": item.id,
        "name": item.name,
        "pizza_type": item.pizza_type,
        "price": item.price,
        "available": item.available,
    }


@router.put("/{item_id}", response_model=MenuItemRead)
def update_menu_item(item_id: int, menu_item: MenuItemUpdate, session: SessionDep):
    item = session.get(MenuItem, item_id)

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    normalized_name = normalize_menu_name(menu_item.name)

    duplicate = find_duplicate_menu_item(
        session=session,
        name=normalized_name,
        pizza_type=menu_item.pizza_type,
        exclude_id=item_id,
    )

    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"Menu item already exists: {duplicate.name} ({duplicate.pizza_type})",
        )

    item.name = normalized_name
    item.category = menu_item.category.strip()
    item.pizza_type = menu_item.pizza_type.strip()
    item.price = menu_item.price
    item.available = menu_item.available

    session.add(item)
    session.commit()
    session.refresh(item)

    return item


@router.delete("/{item_id}")
def delete_menu_item(item_id: int, session: SessionDep):
    item = session.get(MenuItem, item_id)

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    session.delete(item)
    session.commit()

    return {"message": "Menu item deleted"}
