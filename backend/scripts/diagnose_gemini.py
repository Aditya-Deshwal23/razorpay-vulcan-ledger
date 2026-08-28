"""
One-time diagnostic comparison for the real Gemini integration -- not part
of the application, not wired into any phase or the test suite (it makes
real billed API calls and takes real time). Run manually to isolate
whether the 503 tracks the injected temperature default, a specific
model, or neither.

Five configurations, each with a bounded timeout and zero retries so this
script itself cannot hang, run with a short delay between calls:

    1. Raw google-genai SDK,             gemini-3.7-flash, no generation config
    2. LangChain ChatGoogleGenerativeAI, gemini-3.7-flash, temperature=0.7 (LangChain's own default)
    3. LangChain ChatGoogleGenerativeAI, gemini-3.7-flash, temperature=None (this project's current setting)
    4. LangChain ChatGoogleGenerativeAI, gemini-3.6-flash, temperature=0.7 (default)
    5. LangChain ChatGoogleGenerativeAI, gemini-3.5-flash, temperature=0.7 (default)

Never prints the API key -- only PASS/FAIL and the exception type/message.
"""
import asyncio

from config.settings import get_settings

TIMEOUT_SECONDS = 20.0
DELAY_BETWEEN_CALLS_SECONDS = 2.0
PROMPT = "Reply with exactly: DIAGNOSTIC_OK"


async def _run(label: str, coro_factory) -> None:
    print(f"\n--- {label} ---")
    try:
        result = await asyncio.wait_for(coro_factory(), timeout=TIMEOUT_SECONDS)
        print(f"PASS: {result!r}")
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
    await asyncio.sleep(DELAY_BETWEEN_CALLS_SECONDS)


async def main() -> None:
    settings = get_settings()
    # Unwrapped once, here, and never printed: settings.google_api_key is a
    # SecretStr precisely so a traceback out of any of the five calls below
    # renders it as '**********' instead of the credential.
    api_key = settings.require_google_api_key()

    async def raw_sdk_37():
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(TIMEOUT_SECONDS * 1000)),
        )
        response = await client.aio.models.generate_content(model="gemini-3.7-flash", contents=PROMPT)
        return response.text

    async def langchain_call(model: str, temperature):
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
            temperature=temperature,
        )
        response = await llm.ainvoke(PROMPT)
        return response.content

    await _run("1. Raw SDK, gemini-3.7-flash, no generation config", raw_sdk_37)
    await _run("2. LangChain, gemini-3.7-flash, temperature=0.7 (default)", lambda: langchain_call("gemini-3.7-flash", 0.7))
    await _run("3. LangChain, gemini-3.7-flash, temperature=None (this project's fix)", lambda: langchain_call("gemini-3.7-flash", None))
    await _run("4. LangChain, gemini-3.6-flash, temperature=0.7 (default)", lambda: langchain_call("gemini-3.6-flash", 0.7))
    await _run("5. LangChain, gemini-3.5-flash, temperature=0.7 (default)", lambda: langchain_call("gemini-3.5-flash", 0.7))

    print("\nHow to read it:")
    print("  1 fails now              -> something changed since your last test; not code-specific")
    print("  1 passes, 2 fails, 3 passes -> temperature=0.7 was the trigger; this project's fix holds")
    print("  2 and 3 both fail        -> not temperature; 3.7-flash may just be capacity-constrained right now")
    print("  2 fails, 4 or 5 pass     -> specific to gemini-3.7-flash availability, not LangChain generally")
    print("  2, 4, 5 all fail         -> a general LangChain integration issue, not model-specific")


if __name__ == "__main__":
    asyncio.run(main())