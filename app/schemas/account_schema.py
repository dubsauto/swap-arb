# app/schemas/account_schema.py

from pydantic import BaseModel



class AccountLotSchema(BaseModel):
    account_id: int
    lot_size: float

class AllocateSlotRequest(BaseModel):
    user_id:      int
    slot_number:  int
    vps_host:     str
    vps_username: str
    vps_password: str
    vps_port:     int = 22
 
class AddAccountRequest(BaseModel):
    role:             str          # "master" or "slave"
    name:             str
    login:            int
    password:         str
    server:           str
    manual_trades:    bool = True
    use_dedicated_ip: bool = True