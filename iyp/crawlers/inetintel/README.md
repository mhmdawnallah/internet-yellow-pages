# Internet Intelligence Lab - Dataset: AS to Organization mapping -- https://github.com/InetIntel/Dataset-AS-to-Organization-Mapping

The dataset contains historical and current versions of the AS to Organization 
mapping datasets. A mapping will be created between AS to its sibling ASes.

## Graph representation

### Sibling ASes
Connect ASes that are managed by the same organization.
```
(a:AS {asn: 2497})-[:SIBLING_OF]->(b:AS)
```

## Dependence

This crawler is not depending on other crawlers.