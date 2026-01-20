Mock Schneider SmartPDU (Redfish) + Live GUI
============================================

This project provides a **self-contained mock implementation of a Schneider Electric SmartPDU Redfish API**, together with a **real-time GUI** that visualizes outlet-level energy consumption as a heat map.

It is designed specifically to support **energy and carbon (EC) aggregation client development**, integration testing, and visualization — without requiring physical PDU hardware.

Features
--------

### Mock SmartPDU Backend

*   Implements **SmartPDU-style Redfish endpoints**
    
*   Redfish-like payload shapes:
    
    *   @odata.id, @odata.type
        
    *   Collections with Members and Members@odata.count
        
    *   Status { State, Health }
        
    *   Sensors with Reading and ReadingUnits
        
*   Supports:
    
    *   SessionService
        
    *   AccountService
        
    *   Managers
        
    *   PowerEquipment / RackPDUs
        
    *   Outlets, Branches, Mains
        
    *   Power, Energy, Voltage, Current, Frequency sensors
        
    *   EventService subscriptions
        
    *   Load segment power control
        
*   Generates **plausible, internally consistent electrical data**:
    
    *   Power ≈ Voltage × Current
        
    *   Monotonic energy counters
        
    *   PDU totals reconcile with outlet totals
        

### Live GUI (Tkinter)

*   Real desktop GUI (not static plots)
    
*   **2 × 24 outlet layout** (48 outlets total)
    
*   **Heat map coloring based on live outlet power**
    
    *   Blue → low power
        
    *   Yellow → medium power
        
    *   Red → high power
        
*   ON / OFF state clearly indicated
    
*   Live polling from the mock Redfish API
    
*   Optional auto-scaling heat map
    

Repository Structure
--------------------

├── mock_pdu_api.py # FastAPI-based SmartPDU Redfish mock

├── test_mock_pdu_api.py # Pytest test suite

├── pdu_live_gui_heatmap.py # Live GUI with power heat map

├── README.md # Project documentation

Requirements
------------

### Python

*   Python 3.9+
    

### Backend

*   fastapi
    
*   uvicorn
    
*   pytest (for tests)
    

### GUI

*   requests
    
*   Tkinter
    
    *   Included with most Python installations
        
    *   sudo apt install python3-tk
        

Quick Start
-----------

### 1\. Clone the repository

git clone https://github.com//.git  cd` 

### 2\. Install dependencies

pip install fastapi uvicorn requests pytest   `

Running the Mock SmartPDU Backend
---------------------------------

Start the Redfish-style SmartPDU API:

uvicorn mock_pdu_api:app --host 127.0.0.1 --port 8000   `

The API will be available at:

http://127.0.0.1:8000/redfish/v1/   `

### Default credentials

*   Username: admin
    
*   Password: 123456789
    

Basic authentication is required for all GET and DELETE requests.

Running the Live GUI
--------------------

With the backend running in another terminal:

python pdu_live_gui_heatmap.py \    --base-url http://127.0.0.1:8000 \    --pdu-id 2 \    --user admin \    --password 123456789 \    --refresh 1.0 \    --autoscale   `

### GUI behavior

*   Updates every --refresh seconds
    
*   Colors outlets based on **PowerOUTLETn**
    
*   Displays:
    
    *   Outlet number
        
    *   Power (W)
        
    *   Energy (kWh)
        
    *   ON / OFF state
        
*   Auto-scaled heat map adapts to observed load distribution
    

Outlet Layout
-------------

The GUI uses a **2 × 24 layout**:

Column 0 (left):   outlets  1 .. 24  (top → bottom)  Column 1 (right):  outlets 25 .. 48  (top → bottom)   `

This matches common vertical rack PDU physical layouts and makes thermal or power hotspots immediately visible.

Testing the Backend
-------------------

Run the API test suite:

pytest -q   `

The tests validate:

*   Redfish-style payload shapes
    
*   Authentication behavior
    
*   Sensor consistency
    
*   Energy monotonicity
    
*   Load segment power control
    
*   Event subscription lifecycle
    

Intended Use Cases
------------------

*   EC aggregation client development
    
*   Redfish polling logic validation
    
*   Unit and integration testing without hardware
    
*   Power / energy visualization demos
    
*   Training and documentation
    

This project **does not attempt to emulate TLS, certificates, or browser quirks**.It focuses strictly on **client-side integration logic** and data correctness.



## Screenshots

![Mockup](mockup.png)
![Logs](logs.png)

## Exigence Project Introduction

[Watch on YouTube](https://www.youtube.com/watch?v=LcXthE6rZCM&t=50s)
