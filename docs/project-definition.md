# Project Definition

## Problem Statement

There are multiple servers in a network providing different services. A client may need a particular service without knowing which server currently provides it or whether that server is still available.

Our system allows clients to dynamically discover available services and the network locations of the servers providing them, without requiring the client to know those servers beforehand.


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

A single provider can offer multiple services, with each service identified by its service type, transport protocol, and network endpoint.

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
