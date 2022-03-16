FROM archlinux

RUN useradd -r -m -g users proxy

WORKDIR /home/proxy

RUN pacman -Sy --noconfirm sudo && \
    echo "proxy ALL=(ALL) NOPASSWD: /usr/bin/pacman" >> /etc/sudoers && \
    echo "Defaults lecture=never" >> /etc/sudoers && \
    pacman -S --noconfirm --needed base-devel git python2-pip vim && \
    sudo -u proxy git clone https://aur.archlinux.org/stratum-mining-proxy.git  && \
    cd stratum-mining-proxy && \
    sudo -u proxy makepkg -sri --noconfirm --skippgpcheck && \
    cd .. && rm -rf stratum-mining-proxy && \
    pacman -D --asdeps $(pacman -Qqe) && \
    pacman -D --asexplicit stratum-mining-proxy vim && \
    pacman -Rns $(pacman -Qtdq) <<< $'y\n' && \
    rm -rf /var/cache/pacman

EXPOSE 3333

USER proxy

ENTRYPOINT ["stratum-mining-proxy"]
