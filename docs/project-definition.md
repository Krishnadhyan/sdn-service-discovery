# Project Definition

## Problem Statement

There are multiple servers in a network providing different services. A client may need a particular service without knowing which server currently provides it or whether that server is still available.

Our system allows clients to dynamically discover available services and the network locations of the servers providing them, without requiring the client to know those servers beforehand.


## System Overview

Our project implements a Service Discovery System that allows clients to find and communicate with available service providers without knowing their network addresses in advance.

The system consists of five main components:

- **Clients:** Request information about available services from the Service Registry and communicate directly with the selected provider.
- **Service Registry:** Maintains provider records, tracks availability through heartbeats, and returns available providers in response to discovery requests.
- **Service Providers:** Offer services such as FILE, CHAT, and VIDEO. Providers register with the Registry and periodically report their availability.
- **SDN Controller:** Manages network forwarding rules and responds to provider-status changes reported by the Registry.
- **Mininet Network:** Emulates the network containing the clients, Registry, providers, and OpenFlow switches.

The Registry handles service discovery, while the SDN controller manages network traffic rules. These are separate responsibilities that work together to support provider failure handling.

After discovering a provider, the client communicates directly with it. If the provider becomes unavailable, the client can perform discovery again and attempt to use another available provider.



## System Architecture

```text
                  +----------------------+
                  |    SDN Controller    |
                  |   (OpenFlow Rules)   |
                  +----------+-----------+
                             ^
                             | HTTP status events
                             |
+----------+       +---------+----------+
| Client 1 |       |   Service Registry |
+----+-----+       |                    |
     |             | Provider records   |
     | DISCOVER    | Heartbeat tracking |
     +------------>|                    |
     |             +---------+----------+
     |                       ^
     |                       | REGISTER / HEARTBEAT
     |                       |
     |             +---------+----------+
     |             |                    |
     |             |   Service Provider |
     |             |                    |
     |             +--------------------+
     |
     | Direct service communication
     v
+---------------------------+
| Selected Service Provider |
| FILE / CHAT / VIDEO       |
+---------------------------+

 All hosts communicate through the Mininet network,
 using OpenFlow switches controlled by the SDN controller.
```

### Communication Paths

1. **Registration:** Service providers send registration messages to the Registry using UDP.
2. **Availability:** Providers periodically send UDP heartbeat messages to the Registry.
3. **Discovery:** Clients send UDP discovery requests and receive the available provider information from the Registry.
4. **Service communication:** Clients communicate directly with the selected provider using the transport protocol returned by the Registry.
5. **SDN integration:** The Registry sends provider-status events to the SDN controller through an HTTP API. The controller updates the relevant OpenFlow rules on the switches.

The Mininet network emulates the hosts, links, and OpenFlow switches. The SDN controller manages switch behaviour, while the Registry manages service discovery and provider availability.


## Technology Choices and Rationale

### 1. UDP for Service Discovery

UDP is used for registration, heartbeat messages, and service discovery between clients, providers, and the Service Registry.

UDP is connectionless and has low communication overhead, making it suitable for short request-response messages. Since UDP does not guarantee delivery, the application must handle missing responses and timeouts where necessary.

### 2. TCP for FILE and CHAT Services

TCP is used for FILE transfer and CHAT communication because these services require reliable, ordered delivery of data.

TCP handles retransmission of lost segments and delivers data as an ordered byte stream. The application must still define how messages or file contents are framed.

### 3. UDP for VIDEO Service

UDP is used for the simplified VIDEO service to demonstrate a service that communicates using a different transport protocol.

UDP avoids TCP's built-in retransmission and ordering mechanisms, which can be useful for time-sensitive traffic. However, the application must handle any reliability or ordering requirements it needs. Our initial implementation is a demonstration service, not a production video-streaming system.

### 4. Mininet for Network Emulation

Mininet creates a virtual network containing hosts, links, and OpenFlow switches on a single machine.

It allows us to test communication between multiple network nodes, run socket-based applications, and observe network behaviour without requiring separate physical computers and switches.

### 5. SDN and OpenFlow

Software-Defined Networking (SDN) separates network control logic from packet forwarding. The SDN controller determines forwarding behaviour, while OpenFlow allows the controller to manage flow rules on compatible switches.

In our project, the controller responds to provider-status events by installing or removing provider-specific traffic-blocking rules. This demonstrates how service availability information can influence network-level traffic control.

### 6. Centralized Service Registry

A central Registry maintains provider records and answers discovery requests. Clients do not need to know individual provider addresses beforehand; they only need the Registry's configured address.

This simplifies discovery and provides a single place to track provider availability. The Registry is a single point of failure in our initial design, and replication or distributed registries are outside the current scope.

### 7. Heartbeats and Timeouts

Providers periodically send heartbeat messages to the Registry to indicate that they are available.

If a provider's heartbeat is not received within the configured timeout, the Registry marks it as inactive and excludes it from future discovery responses. This allows the system to detect suspected failures without requiring clients to continuously check every provider.

A heartbeat timeout indicates that a provider is considered unavailable; it does not necessarily prove that the provider has permanently failed.




## Expected Demonstration and Validation

The project demonstration will show how clients discover services dynamically, communicate with selected providers, and recover when a provider becomes unavailable. It will also demonstrate how provider availability changes can trigger network-level actions through the SDN controller.

### Scenario 1: Provider Registration

1. Start the Service Registry and service providers.
2. Providers register their service type, transport protocol, and port with the Registry.
3. The Registry records the provider information and marks the provider as active.
4. Verify that the Registry has recorded the expected provider information.

**Expected result:** The Registry contains the registered providers and their service endpoints.

### Scenario 2: Service Discovery

1. A client requests the FILE service using a UDP DISCOVER message.
2. The Registry searches its active provider records.
3. The Registry returns all active FILE providers in a single response.
4. The client selects the first provider returned and connects directly to it.

**Expected result:** The client discovers providers without needing their addresses in advance and can communicate with the selected provider.

### Scenario 3: Different Service Types

Demonstrate discovery and communication for FILE, CHAT, and VIDEO using their configured transport protocols.

**Expected result:** The same discovery mechanism supports multiple service types, while actual service communication uses the protocol specified by each provider.

### Scenario 4: Service Unavailability

Request a service for which no active provider is registered.

**Expected result:** The Registry returns a `SERVICE_UNAVAILABLE` response rather than returning an inactive provider as available.

### Scenario 5: Provider Failure and Rediscovery

1. Discover FILE providers and select FILE-1.
2. Simulate a provider failure or stop its heartbeat sender.
3. Allow the Registry's heartbeat timeout to expire.
4. Verify that the Registry marks FILE-1 as inactive and excludes it from future discovery responses.
5. The Registry sends an INACTIVE status event to the SDN controller.
6. Verify that the controller installs the corresponding OpenFlow drop rule.
7. The client performs discovery again and selects FILE-2.
8. Verify that the client can communicate with FILE-2.

**Expected result:** The system detects the unavailable provider, updates discovery information, demonstrates the SDN rule change, and allows the client to recover through another provider.

### Scenario 6: Provider Recovery

Restore the provider's heartbeat and allow the Registry to recognize it as active according to the implemented recovery mechanism.

**Expected result:** The Registry can advertise the recovered provider again, and the controller removes the corresponding drop rule.

### Validation Evidence

The demonstration should provide observable evidence wherever applicable:

- Registry logs showing registration, discovery, and provider-status changes.
- Client output showing the discovered provider and service communication.
- Registry responses showing that inactive providers are excluded.
- Switch flow-table output showing the installation and removal of the relevant OpenFlow rule.
- Mininet connectivity and service tests confirming the expected behaviour.

Features will be considered demonstrated only after their behaviour has been tested in the implemented system.


## Service Registry Location

The Service Registry runs as a dedicated network service on a known host.

Clients and service providers are configured with the Registry's IP address and port.

Clients send DISCOVER requests to the Registry, while service providers send registration and availability messages to the Registry.




## System Scope

Our project will implement one Service Discovery System within a single controlled network/domain.

The system will contain multiple clients and service providers, with the Service Registry acting as the central discovery infrastructure for the network.

The Registry will support multiple service types and multiple providers for the same service.

The project does not aim to solve how a device discovers service-discovery infrastructure across the Internet or across different administrative domains. That infrastructure is considered to be part of the network environment.



## Supported Service Types

The Service Discovery System will support multiple service types.

For the initial implementation and demonstration, the system will include:

- FILE — file transfer service
- CHAT — messaging service
- VIDEO — video/streaming service

These services are simplified implementations intended to demonstrate service discovery and network communication. The project does not attempt to recreate full-scale applications such as WhatsApp or other production services.

The same Service Discovery mechanism is used regardless of the service type.

The transport protocol used for actual service communication may differ between services depending on their requirements.




## Service Providers

Each service provider is modeled as an independent service instance.

A provider host provides one service type rather than hosting all service types.

The initial Mininet network will contain multiple clients, a Service Registry, and multiple service-provider hosts.

Example:

h1 → Client 1
h2 → Client 2
h3 → Service Registry
h4 → FILE Provider 1
h5 → FILE Provider 2
h6 → CHAT Provider 1
h7 → VIDEO Provider 1

Multiple providers may provide the same service. This allows the Service Registry to return multiple available providers for a requested service and allows provider failure and rediscovery to be demonstrated.

For example:

FILE Provider 1 → FILE → TCP → 10.0.0.4:5001
FILE Provider 2 → FILE → TCP → 10.0.0.5:5001
CHAT Provider 1 → CHAT → TCP → 10.0.0.6:5002
VIDEO Provider 1 → VIDEO → UDP → 10.0.0.7:5003




## Provider Registration

Each service provider has a unique Provider ID within our system.

The Provider ID identifies a logical service-provider instance, not necessarily a physical machine.

Each provider host offers one service type in the initial implementation. Each service instance is identified by its Provider ID, service type, transport protocol, and network endpoint. Multiple provider instances may offer the same service type.
The transport protocol specifies how the client should communicate with the actual service after discovery.

For example:

Provider-A | FILE    | TCP | 10.0.0.5 | 5001
Provider-A | VIDEO   | UDP | 10.0.0.5 | 5002
Provider-A | CHAT | TCP | 10.0.0.5 | 5003







## Service Discovery Flow

A client discovers a service by sending a DISCOVER request to the Service Registry.

The client specifies the type of service it requires. The Registry searches its current provider records and returns the available providers offering that service.

For example:

DISCOVER | FILE

The Registry may return:

Provider-A | TCP | 10.0.0.5 | 5001
Provider-B | TCP | 10.0.0.6 | 5001

For the basic implementation, the client selects the first available provider returned by the Registry.

The client then communicates directly with the selected service provider. The Registry is used for discovery and is not involved in the actual service communication.

The overall flow is:

Provider → REGISTER → Registry
Client → DISCOVER → Registry
Client ← Provider information ← Registry
Client → Service Provider



## Service Unavailability

If a client requests a service for which no active provider is currently
registered, the Service Registry returns a service-unavailable response.

For example:

DISCOVER | VIDEO

If no active VIDEO provider is available:

ERROR | SERVICE_UNAVAILABLE | VIDEO

The client can then handle the unavailable service appropriately.



## Basic Communication Protocol

The system uses UDP for communication between clients, service providers, and the Service Registry for service discovery and registration.

The transport protocol used for actual service communication depends on the service being provided.

### Provider Registration

A provider registers a service with the Registry using:

REGISTER | Provider ID | Service | Protocol | Port

Example:

REGISTER | Provider-A | FILE | TCP | 5001

The Registry obtains the provider's IP address from the incoming UDP packet and records the provider as active.

The Registry confirms a successful registration using:

REGISTER_OK | Provider ID | Service

Example:

REGISTER_OK | Provider-A | FILE

### Service Discovery

A client requests a service using:

DISCOVER | Service

Example:

DISCOVER | FILE

The Registry searches its active provider records and returns all available providers for the requested service in a single response.

Example:

DISCOVER_RESPONSE | FILE | Provider-A,TCP,10.0.0.5,5001 | Provider-B,TCP,10.0.0.6,5001

For the basic implementation, the client selects the first available provider returned by the Registry.

If no active provider is available for the requested service, the Registry returns:

ERROR | SERVICE_UNAVAILABLE | Service

Example:

ERROR | SERVICE_UNAVAILABLE | FILE



## Actual Service Communication

After discovering a suitable provider, the client communicates directly with the selected service provider.
The Service Registry is not involved in the actual transfer of service data.
The transport protocol used for the service communication is determined by the service.

For example:

FILE service:

Client → Provider using TCP

VIDEO service:

Client → Provider using UDP or another appropriate transport protocol

The service discovery protocol remains UDP regardless of the transport protocol used by the actual service.


## Provider Availability

Service providers periodically send heartbeat messages to the Service Registry.

Example:

HEARTBEAT | Provider-A

The Registry records the most recent heartbeat from each provider.

If a provider fails to send a heartbeat within the configured timeout period, the Registry marks the provider as unavailable and excludes it from future discovery responses.



## Provider Failure After Discovery

If a client discovers a provider but the provider becomes unavailable before or during service communication, the client detects the communication failure and performs a new service discovery request.

The client then selects an available provider from the new discovery response and retries the request.





## Mininet Topology and IP Addressing

### Network Overview

The system is implemented in a simulated network using Mininet. The topology consists of seven hosts connected through two OpenFlow switches, `s1` and `s2`. The switches are managed by an SDN controller using the OpenFlow protocol.

The network uses the `10.0.0.0/24` subnet. Each host is assigned a static IP address to simplify service registration, discovery, and communication.

### Host Configuration

| Host | Role | IP Address | Service Port |
|---|---|---|---|
| h1 | Client 1 | 10.0.0.1 | — |
| h2 | Client 2 | 10.0.0.2 | — |
| h3 | Service Registry | 10.0.0.3 | UDP 5000 |
| h4 | FILE Provider 1 | 10.0.0.4 | TCP 5001 |
| h5 | FILE Provider 2 | 10.0.0.5 | TCP 5001 |
| h6 | CHAT Provider 1 | 10.0.0.6 | TCP 5002 |
| h7 | VIDEO Provider 1 | 10.0.0.7 | UDP 5003 |

The Service Registry listens for registration, heartbeat, and discovery messages on UDP port 5000. Provider service ports are separate from the Registry's discovery port.

### Topology and Connectivity

The hosts are connected to the switches as follows:

- **Switch s1:** Connects h1, h2, and h3.
- **Switch s2:** Connects h4, h5, h6, and h7.
- **Inter-switch link:** Connects s1 and s2, allowing clients and the Registry to communicate with service providers across the network.
- **SDN controller:** Controls s1 and s2 through OpenFlow and manages forwarding rules based on provider availability events.

### SDN Integration

The SDN controller manages the forwarding behaviour of the OpenFlow switches. When the Service Registry detects that a provider has become unavailable, it notifies the controller. The controller installs a provider-specific flow rule to block traffic to the unavailable service endpoint. When the provider becomes active again, the corresponding rule is removed.

The Service Registry remains responsible for maintaining provider records and responding to discovery requests, while the SDN controller manages network-level traffic rules.

### Design Scope

The topology represents a single controlled network domain. Clients are configured with the Service Registry's address (`10.0.0.3:5000`) and do not need to know provider addresses in advance. Provider addresses and service ports are returned dynamically through discovery responses.

The topology contains a single link between the two switches; therefore, the current design demonstrates provider-specific traffic filtering and service recovery rather than alternate-path rerouting.
