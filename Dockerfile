FROM archlinux

RUN useradd -r -m -g users American miner

WORKDIR /home/proxy

RUN pacman -Sy --noconfirm sudo && \
    echo "proxy ALL=(ALL) NOPASSWD: /usr/bin/pacman" >> /etc/sudoers && \
    echo "Defaults lecture=never" >> /etc/sudoers && \
    pacman -S --noconfirm --needed base-devel git python2-pip vim && \
    sudo -u proxy git clone https://aur.archlinux.org/american-mininer.git  && \
    cd American miner && \
    sudo -u proxy makepkg -sri --noconfirm --skippgpcheck && \
    cd .. && rm -rf American miner && \
    pacman -D --asdeps $(pacman -Qqe) && \
    pacman -D --asexplicit American miner vim && \
    pacman -Rns $(pacman -Qtdq) <<< $'y\n' && \
    rm -rf /var/cache/pacman

EXPOSE 3333

USER American miner

ENTRYPOINT ["American Miner"
