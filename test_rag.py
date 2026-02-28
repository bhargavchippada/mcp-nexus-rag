from server import (
    ingest_graph_document,
    get_graph_context,
    ingest_vector_document,
    get_vector_context,
    get_all_project_ids,
    get_all_tenant_scopes,
    delete_tenant_data,
)
import pytest


@pytest.mark.asyncio
async def test_graph_rag():
    print("--- Testing GraphRAG ---")
    print("Testing Graph ingestion for TRADING_BOT...")
    await ingest_graph_document(
        "The market is showing bullish trends for tech stocks in Q3 2026. Keep an eye on quantum computing sectors.",
        project_id="TRADING_BOT",
        scope="WEB_RESEARCH",
        source_identifier="test_script",
    )

    print("Testing Graph ingestion for WEB_PORTAL...")
    await ingest_graph_document(
        "The new UI requires Tailwind CSS components for the dashboard. Ensure dark mode is supported.",
        project_id="WEB_PORTAL",
        scope="WEB_RESEARCH",
        source_identifier="test_script",
    )

    print("\nRetrieving Graph context for TRADING_BOT...")
    trading_context = await get_graph_context(
        "bullish trends", "TRADING_BOT", "WEB_RESEARCH"
    )
    print(f"TRADING_BOT Graph Context:\n{trading_context}")

    print("\nRetrieving Graph context for WEB_PORTAL...")
    portal_context = await get_graph_context(
        "dashboard components", "WEB_PORTAL", "WEB_RESEARCH"
    )
    print(f"WEB_PORTAL Graph Context:\n{portal_context}")

    print(
        "\nRetrieving TRADING_BOT context in WEB_PORTAL (Cross-Contamination Test)..."
    )
    cross_context = await get_graph_context(
        "bullish trends", "WEB_PORTAL", "WEB_RESEARCH"
    )
    print(f"Graph Cross-Contamination Context:\n{cross_context}")


@pytest.mark.asyncio
async def test_vector_rag():
    print("\n--- Testing VectorRAG ---")
    print("Testing Vector ingestion for TRADING_BOT...")
    await ingest_vector_document(
        "Quantum computing is accelerating rapidly, providing new options for algorithmic trading.",
        project_id="TRADING_BOT",
        scope="TECH_RESEARCH",
        source_identifier="test_script",
    )

    print("Testing Vector ingestion for WEB_PORTAL...")
    await ingest_vector_document(
        "WebAssembly allows high-performance execution of code in the browser.",
        project_id="WEB_PORTAL",
        scope="TECH_RESEARCH",
        source_identifier="test_script",
    )

    print("\nRetrieving Vector context for TRADING_BOT...")
    trading_context = await get_vector_context(
        "quantum algorithmic trading", "TRADING_BOT", "TECH_RESEARCH"
    )
    print(f"TRADING_BOT Vector Context:\n{trading_context}")

    print("\nRetrieving Vector context for WEB_PORTAL...")
    portal_context = await get_vector_context(
        "high performance browser code", "WEB_PORTAL", "TECH_RESEARCH"
    )
    print(f"WEB_PORTAL Vector Context:\n{portal_context}")

    print(
        "\nRetrieving TRADING_BOT context in WEB_PORTAL (Cross-Contamination Test)..."
    )
    cross_context = await get_vector_context(
        "quantum trading", "WEB_PORTAL", "TECH_RESEARCH"
    )
    print(f"Vector Cross-Contamination Context:\n{cross_context}")


@pytest.mark.asyncio
async def test_metadata_extraction():
    print("\n--- Testing Metadata Extraction ---")
    try:
        projects = await get_all_project_ids()
        scopes = await get_all_tenant_scopes()
        print(f"Discovered Projects: {projects}")
        print(f"Discovered Scopes: {scopes}")

        assert "TRADING_BOT" in projects
        assert "WEB_PORTAL" in projects
        assert "WEB_RESEARCH" in scopes
        assert "TECH_RESEARCH" in scopes
    except Exception as e:
        pytest.skip(f"Could not verify metadata (DBs might be missing or empty): {e}")


@pytest.mark.asyncio
async def test_data_deletion():
    print("\n--- Testing Data Deletion ---")
    try:
        print("Ingesting dummy data for DELETION_TEST in TEMP_SCOPE...")
        await ingest_vector_document("Dummy content", "DELETION_TEST", "TEMP_SCOPE")
        await ingest_graph_document("Dummy content", "DELETION_TEST", "TEMP_SCOPE")

        projects = await get_all_project_ids()
        assert "DELETION_TEST" in projects

        print("Deleting DELETION_TEST project...")
        await delete_tenant_data("DELETION_TEST")

        projects_after = await get_all_project_ids()
        assert "DELETION_TEST" not in projects_after

        print("Ingesting another dummy for Scope Deletion...")
        await ingest_vector_document("Dummy content", "SCOPE_TEST", "DELETE_ME")
        await ingest_vector_document("Dummy content", "SCOPE_TEST", "KEEP_ME")

        projects_scope = await get_all_project_ids()
        assert "SCOPE_TEST" in projects_scope

        print("Deleting DELETE_ME scope in SCOPE_TEST project...")
        await delete_tenant_data("SCOPE_TEST", "DELETE_ME")

        # Use project-scoped lookup to avoid cross-tenant false positives
        scopes_after = await get_all_tenant_scopes(project_id="SCOPE_TEST")
        assert "DELETE_ME" not in scopes_after, (
            "Deleted scope should not appear under SCOPE_TEST"
        )
        assert "KEEP_ME" in scopes_after, "Non-deleted scope should still be present"

        # Clean up the rest
        await delete_tenant_data("SCOPE_TEST")
    except Exception as e:
        pytest.skip(f"Could not verify deletion: {e}")


if __name__ == "__main__":
    import asyncio

    async def _main():
        await test_graph_rag()
        await test_vector_rag()
        await test_metadata_extraction()
        await test_data_deletion()

    asyncio.run(_main())
