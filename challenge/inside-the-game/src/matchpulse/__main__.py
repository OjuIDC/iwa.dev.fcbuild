import uvicorn


def main() -> None:
    uvicorn.run("matchpulse.service:app", host="0.0.0.0", port=8080, reload=False)


if __name__ == "__main__":
    main()
