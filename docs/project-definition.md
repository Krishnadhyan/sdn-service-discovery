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



## Provider Registration

Each service provider has a unique Provider ID within our system.

When registering, the provider supplies its Provider ID, service type,
and service port. The Registry obtains the provider's IP address from
the incoming network connection and initially marks the provider as active.

Conceptually:

REGISTER | Provider ID | Service | Port



## Provider ID

Each service provider has a unique Provider ID within our system.

The Provider ID identifies a logical service-provider instance, not necessarily a physical machine.

A single provider can offer multiple services, with each service identified by its own service type and network endpoint.

For example:

Provider-A | FILE  | 10.0.0.5 | 5001
Provider-A | VIDEO | 10.0.0.5 | 5002



## Service Discovery Flow

A client discovers a service by sending a DISCOVER request to the Service Registry.

The client specifies the type of service it requires. The Registry searches its current provider records and returns the available providers offering that service.

For example:

DISCOVER | FILE

The Registry may return:

Provider-A | 10.0.0.5 | 5001
Provider-B | 10.0.0.6 | 5001

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
