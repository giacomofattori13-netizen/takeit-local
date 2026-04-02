from sqlmodel import SQLModel, Field
from typing import List


class MenuItemCreate(SQLModel):
    name: str
    category: str
    pizza_type: str
    price: float
    available: bool = True


class MenuItemRead(SQLModel):
    id: int
    name: str
    category: str
    pizza_type: str
    price: float
    available: bool


class MenuItemUpdate(SQLModel):
    name: str
    category: str
    pizza_type: str
    price: float
    available: bool = True


class OrderItemCreate(SQLModel):
    pizza_name: str
    pizza_type: str
    quantity: int = Field(gt=0)
    add_ingredients: list[str] = []
    remove_ingredients: list[str] = []


class OrderCreate(SQLModel):
    customer_name: str
    pickup_time: str
    items: List[OrderItemCreate]


class OrderItemRead(SQLModel):
    id: int
    order_id: int
    pizza_name: str
    pizza_type: str
    quantity: int
    add_ingredients: list[str] = []
    remove_ingredients: list[str] = []


class OrderRead(SQLModel):
    id: int
    customer_name: str
    pickup_time: str
    status: str
    items: List[OrderItemRead]

class OrderStatusUpdate(SQLModel):
    status: str


class ChatRequest(SQLModel):
    session_id: str
    message: str


class ChatResponse(SQLModel):
    session_id: str
    user_message: str
    extracted_order: dict
    merged_order: dict
    valid: bool
    missing_items: list[str]
    response_message: str
    order_id: int | None = None
    state: str


class SessionCreateRequest(SQLModel):
    caller_phone: str | None = None


class SessionRead(SQLModel):
    session_id: str
    customer_name: str | None
    customer_phone: str | None
    pickup_time: str | None
    items: list[dict]
    completed: bool
    state: str

class SessionCreateResponse(SQLModel):
    session_id: str
    state: str
    completed: bool

class ChatStartResponse(SQLModel):
    session_id: str
    state: str
    completed: bool
    response_message: str
