import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Order, OrderItem
from app.schemas import OrderRead, OrderItemRead, OrderStatusUpdate
from app.security import require_admin_api_key

router = APIRouter(
    prefix="/orders",
    tags=["orders"],
    dependencies=[Depends(require_admin_api_key)],
)

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/", response_model=list[OrderRead])
def list_orders(session: SessionDep):
    statement = select(Order)
    orders = session.exec(statement).all()

    result = []
    for order in orders:
        items_statement = select(OrderItem).where(OrderItem.order_id == order.id)
        order_items = session.exec(items_statement).all()

        result.append(
            OrderRead(
                id=order.id,
                customer_name=order.customer_name,
                pickup_time=order.pickup_time,
                status=order.status,
                items=[
                    OrderItemRead(
                        id=item.id,
                        order_id=item.order_id,
                        pizza_name=item.pizza_name,
                        pizza_type=item.pizza_type,
                        quantity=item.quantity,
                        add_ingredients=json.loads(item.add_ingredients_json),
                        remove_ingredients=json.loads(item.remove_ingredients_json),
                    )
                    for item in order_items
                ],
            )
        )

    return result


@router.patch("/{order_id}/status", response_model=OrderRead)
def update_order_status(order_id: int, payload: OrderStatusUpdate, session: SessionDep):
    allowed_statuses = {"new", "preparing", "ready", "completed"}

    if payload.status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid status")

    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = payload.status
    session.add(order)
    session.commit()
    session.refresh(order)

    statement = select(OrderItem).where(OrderItem.order_id == order.id)
    order_items = session.exec(statement).all()

    return OrderRead(
        id=order.id,
        customer_name=order.customer_name,
        pickup_time=order.pickup_time,
        status=order.status,
        items=[
            OrderItemRead(
                id=item.id,
                order_id=item.order_id,
                pizza_name=item.pizza_name,
                pizza_type=item.pizza_type,
                quantity=item.quantity,
                add_ingredients=json.loads(item.add_ingredients_json),
                remove_ingredients=json.loads(item.remove_ingredients_json),
            )
            for item in order_items
        ],
    )
