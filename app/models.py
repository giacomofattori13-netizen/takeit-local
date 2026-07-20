from typing import Optional
from sqlmodel import SQLModel, Field


class MenuItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    category: str
    pizza_type: str
    price: float
    available: bool = True
    sale_unit: str = Field(default="piece")
    restaurant_id: Optional[str] = None


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_session_id: Optional[str] = Field(default=None, index=True)
    customer_name: str
    pickup_time: str
    pickup_date: Optional[str] = None  # "YYYY-MM-DD" — preorder date (pizza al taglio)
    status: str = "new"


class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id")
    pizza_name: str
    pizza_type: str
    quantity: float = Field(default=1.0)
    add_ingredients_json: str = "[]"
    remove_ingredients_json: str = "[]"
    dough_type: str = "classica"
    size: str = "normale"
    sale_unit: str = Field(default="piece")
    temperature: Optional[str] = None
    portion: Optional[str] = None  # "piena" | "mezza" — slice size label (kg items only)


class OrderSideEffect(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_number: int = Field(index=True)
    kind: str = Field(index=True)
    payload_json: str
    status: str = Field(default="pending", index=True)
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: Optional[str] = None
    created_at: float = Field(default=0.0)
    updated_at: float = Field(default=0.0)


class ConversationSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, unique=True)
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    pickup_time: Optional[str] = None
    items_json: str = "[]"
    suggested_items_json: str = "[]"
    state: str = "collecting_items"
    completed: bool = False
    intended_quantity: Optional[int] = None
    pending_customer_name: Optional[str] = None
    favorite_pizzas_json: str = "[]"
    no_input_count: int = 0
    reservation_json: str = "{}"
    restaurant_id: Optional[str] = None
    kg_temperature: Optional[str] = None

class ConversationLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    user_message: str
    extracted_order_json: str
    merged_order_json: str
    response_message: str
    valid: bool
    missing_items_json: str = "[]"
    state: str
