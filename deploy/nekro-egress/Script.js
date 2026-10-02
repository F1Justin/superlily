// Define main function (script entry)

function main(config, profileName) {
  const direct = [
    "DOMAIN,nursing.f1justin.com,DIRECT",
    "DOMAIN-SUFFIX,argotunnel.com,DIRECT",
    "DOMAIN-SUFFIX,cftunnel.com,DIRECT",
    "IP-CIDR,198.41.192.0/24,DIRECT,no-resolve",
    "IP-CIDR,198.41.200.0/24,DIRECT,no-resolve",
  ];
  config.rules = [...direct, ...(config.rules || []).filter(rule => !direct.includes(rule))];
  config.dns = config.dns || {};
  config.dns["fake-ip-filter"] = [...new Set([...(config.dns["fake-ip-filter"] || []), "+.argotunnel.com", "+.cftunnel.com"])];
  config.dns["nameserver-policy"] = {
    ...(config.dns["nameserver-policy"] || {}),
    "+.argotunnel.com": ["https://cloudflare-dns.com/dns-query"],
    "+.cftunnel.com": ["https://cloudflare-dns.com/dns-query"],
  };
  const nodes = (config.proxies || [])
    .filter(p => /\.JP |\.SG |日本|新加坡/.test(p.name))
    .map(p => p.name)
    .sort((a, b) => Number(!a.includes("AnyTLS")) - Number(!b.includes("AnyTLS")) ||
      Number(!a.includes(".JP ")) - Number(!b.includes(".JP ")) || a.localeCompare(b));
  if (nodes.length) {
    config["proxy-groups"] = (config["proxy-groups"] || []).filter(g => !["Nekro-Auto", "Nekro-Egress"].includes(g.name));
    config["proxy-groups"].push({name: "Nekro-Auto", type: "fallback", proxies: nodes,
      url: "https://openrouter.ai/api/v1/models", "expected-status": 200,
      interval: 30, timeout: 5000, lazy: false});
    config["proxy-groups"].push({name: "Nekro-Egress", type: "select", proxies: ["Nekro-Auto", ...nodes]});
    config.listeners = (config.listeners || []).filter(l => !l.name.startsWith("nekro-ai"));
    config.listeners.push({name: "nekro-ai", type: "mixed", listen: "172.20.0.1", port: 7898,
      udp: false, proxy: "Nekro-Egress"});
    nodes.forEach((name, index) => config.listeners.push({name: "nekro-ai-node-" + index,
      type: "mixed", listen: "172.20.0.1", port: 18000 + index, udp: false, proxy: name}));
  }
  return config;
}
