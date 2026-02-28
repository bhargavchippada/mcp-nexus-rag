import os
from pyvis.network import Network
from neo4j import GraphDatabase
import webbrowser

# Neo4j connection details
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

def fetch_graph_data():
    """Fetches nodes and relationships from Neo4j."""
    query = """
    MATCH (n)-[r]->(m)
    RETURN n, r, m
    LIMIT 1000
    """
    
    nodes = {}
    edges = []
    
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        result = session.run(query)
        for record in result:
            n = record["n"]
            m = record["m"]
            r = record["r"]
            
            # Extract node IDs and Labels
            n_id = n.element_id
            m_id = m.element_id
            
            # Format Node labels and properties for display
            n_label = list(n.labels)[0] if n.labels else "Node"
            n_name = n.get("id", str(n_id))
            
            m_label = list(m.labels)[0] if m.labels else "Node"
            m_name = m.get("id", str(m_id))
            
            nodes[n_id] = {"label": f"{n_label}\n{n_name}", "group": n_label}
            nodes[m_id] = {"label": f"{m_label}\n{m_name}", "group": m_label}
            
            # Edge formatting
            edges.append((n_id, m_id, type(r).__name__))

    driver.close()
    return nodes, edges

def create_visualization():
    """Generates an interactive HTML visualization of the Neo4j Graph."""
    print("Fetching data from Neo4j...")
    nodes, edges = fetch_graph_data()
    
    if not nodes:
        print("No data found in Neo4j. Have you ingested any documents yet?")
        return

    print(f"Building graph with {len(nodes)} nodes and {len(edges)} edges...")
    
    # Initialize PyVis Network
    net = Network(height="800px", width="100%", bgcolor="#222222", font_color="white", directed=True)
    
    # Add Nodes
    for n_id, data in nodes.items():
        net.add_node(n_id, label=data["label"], title=data["label"], group=data["group"])
        
    # Add Edges
    for edge in edges:
        net.add_edge(edge[0], edge[1], title=edge[2], label=edge[2])
        
    # Enable Physics for better layout
    net.force_atlas_2based()
    net.show_buttons(filter_=['physics'])
    
    output_file = "nexus_graph.html"
    net.save_graph(output_file)
    print(f"Visualization saved to {os.path.abspath(output_file)}")
    
    # Open in browser automatically
    webbrowser.open(f"file://{os.path.abspath(output_file)}")

if __name__ == "__main__":
    create_visualization()
