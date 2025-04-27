# AnyRelay

AnyRelay is a Python project to dynamically create ClashMeta/Mihomo proxy configurations containing relay nodes towards the target nodes.

## Purpose

This project is designed and developed for learning, research and security testing purposes only. It aims to provide a tool for security researchers, academics and technology enthusiasts to understand and practice network communication technology.

## Legality

Users must comply with local laws and regulations when downloading and using the project. Users are responsible for ensuring that their actions comply with the laws, regulations and other applicable regulations in their region.

## Basic Usage

1. Edit `data/nodes.csv`
2. Run `python scripts/regenerate_ini.py`
3. Git add, commit, and push
4. (optional) Restart the subconverter backend(e.g., docker stop & docker rm & docker run), if one needs to clear the cached files or remove logs and wishes to take this update immediately.
5. Refresh the subscribtion urls on ClashMeta/Mihomo clients.

## Dependencies

1. A subconverter frontend that supports customized backend.
2. A subconverter backend that supports explicitly anoucement of loadbalance strategies of `loadbalance` nodes.
3. One or more relay target nodes whose names started with `RelayTarget`.
   1. Relay target can be `shadowsocks` or `socks` nodes. `shadowsocks` nodes can be used if user wants to customize the outbound proxy(wireguard, for example) in backends like Xray, yet may cause higher computation cost in encryption and decrypition processes. Instead, `socks` nodes is much simpler yet may have higher latency. I recommend creating `socks` nodes via [Dante](https://www.digitalocean.com/community/tutorials/how-to-set-up-dante-proxy-on-ubuntu-20-04).
   2. Pls remind that `socks` nodes without username/password and tls are not secure.

## Project Tree

1. `config`: Final ClashMeta/Mihomo proxy configurations.
   1. `relay.ini`: Relay configuration that supports `round-robin` strategy in `loadbalance` nodes.
   2. `relay_no_lb.ini`: Relay configuration that uses `url-test` instead of `loadbalance`.
   3. `nodnsleak.ini`: Non-Relay configuration that prohibit DNS leak.
2. `data`: Node data and configuration template.
3. `scripts`: Scripts to generate configuration.
4. `rules`: Specified relay/reject rules, for personal usages.

## Development Plan

1. Add support for [dialer nodes](https://wiki.metacubex.one/en/config/proxies/#dialer-proxy) as `relay` is about to be [deprecated](https://wiki.metacubex.one/en/config/proxy-groups/relay/) in future release of mihomo.

### Disclaimer

1. As the author of this project, I (hereinafter referred to as the "author") emphasize that this project should be used only for legal, ethical and educational purposes.
2. The author does not encourage, support or promote any form of illegal use of this project. If it is found that this project is used for illegal or immoral activities, the author will strongly condemn such behavior.
3. The author is not responsible for any illegal activities carried out by any person or group using this project. Any consequences arising from the use of this project by the user shall be borne by the user himself.
4. The author is not responsible for any direct or indirect damages that may arise from the use of this project.
5. By using this project, the user understands and agrees to all the terms of this disclaimer. If the user does not agree to these terms, he should stop using the project immediately.
6. The author reserves the right to update this disclaimer at any time without prior notice. The latest version of the disclaimer will be published on the project's GitHub page.
