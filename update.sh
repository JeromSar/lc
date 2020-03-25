#!/bin/bash

configs=(
	~/.vscode
	~/.tmux.conf
	/etc/vim/vimrc
	/etc/vim/vimrc.local
)

for c in "${configs[@]}"; do
	if [[ -a $c ]];
	then
		echo "Copying over $c..."
		cp -r -u --no-preserve=ownership "$c" .
	else
		echo "No $c present"
	fi

done

