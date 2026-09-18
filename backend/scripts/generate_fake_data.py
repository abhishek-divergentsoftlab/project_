import sys
import os

# Add parent dir to path to import services
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.qdrant_service import init_qdrant, add_rfq

def generate_fake_data():
    init_qdrant()
    # Using dummy vectors since embeddings require a model
    # Mocking size=384
    fake_buyers = [
        {
            "role": "buyer",
            "category": "Electronics",
            "title": "Need 6000 unit of red data cable of type c",
            "price_target": 200,
            "location": "Indore",
            "deadline_days": 3
        },
        {
            "role": "buyer",
            "category": "Processors",
            "title": "Buying i9 chips of Intel 10th gen",
            "price_target": 25000,
            "location": "Mumbai",
            "deadline_days": 10
        }
    ]
    
    fake_sellers = [
        {
            "role": "seller",
            "category": "Stationery",
            "title": "Supplying permanent marker of black color",
            "price_target": 15,
            "location": "Delhi",
            "stock": 50000
        },
        {
            "role": "seller",
            "category": "Electronics",
            "title": "Selling red data cables type C",
            "price_target": 180,
            "location": "Indore",
            "stock": 10000
        }
    ]
    
    print("Generating fake data...")
    for item in fake_buyers + fake_sellers:
        dummy_vector = [0.1] * 384  # Placeholder for real embedding
        add_rfq(item, dummy_vector)
        print(f"Added {item['role']} record: {item['title']}")
    
    print("Fake data generation complete.")

if __name__ == "__main__":
    generate_fake_data()
