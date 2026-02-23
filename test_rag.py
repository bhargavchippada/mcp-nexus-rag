import asyncio
from server import ingest_document, get_context

async def main():
    print("Testing ingestion for TRADING_BOT...")
    await ingest_document(
        "The market is showing bullish trends for tech stocks in Q3 2026. Keep an eye on quantum computing sectors.",
        project_id="TRADING_BOT",
        scope="WEB_RESEARCH",
        source_identifier="test_script"
    )

    print("Testing ingestion for WEB_PORTAL...")
    await ingest_document(
        "The new UI requires Tailwind CSS components for the dashboard. Ensure dark mode is supported.",
        project_id="WEB_PORTAL",
        scope="WEB_RESEARCH",
        source_identifier="test_script"
    )

    print("\nRetrieving context for TRADING_BOT...")
    trading_context = await get_context("bullish trends", "TRADING_BOT", "WEB_RESEARCH")
    print(f"TRADING_BOT Context:\n{trading_context}")
    
    print("\nRetrieving context for WEB_PORTAL...")
    portal_context = await get_context("dashboard components", "WEB_PORTAL", "WEB_RESEARCH")
    print(f"WEB_PORTAL Context:\n{portal_context}")
    
    print("\nRetrieving TRADING_BOT context in WEB_PORTAL (Cross-Contamination Test)...")
    cross_context = await get_context("bullish trends", "WEB_PORTAL", "WEB_RESEARCH")
    print(f"Cross-Contamination Context:\n{cross_context}")

if __name__ == "__main__":
    asyncio.run(main())
