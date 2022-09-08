import uvicorn
from fastapi import FastAPI, WebSocket
from protocols.my_proto import IudeenProto

app = FastAPI()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        print(data)
        await websocket.send_text(f"Message text was: {data}")


if __name__ == "__main__":
    uvicorn.run("app.main:app", ws=IudeenProto)

