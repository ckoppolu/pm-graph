#!/bin/sh

if [ $# -lt 1 ]; then
	echo "googlesheet multisheet processer"
	echo "USAGE: multitest <tarball>"
	exit
elif [ ! -e $1 ]; then
	echo "ERROR: $1 does not exist"
	exit
elif [ -d $1 ]; then
	echo "ERROR: $1 is a directory, not a tarball"
	exit
fi
export https_proxy="https://proxy-chain.intel.com:912/"
export http_proxy="http://proxy-chain.intel.com:911/"
export no_proxy="intel.com,.intel.com,localhost,127.0.0.1"
export socks_proxy="socks://proxy-us.intel.com:1080/"
export ftp_proxy="ftp://proxy-chain.intel.com:911/"

# get least used /media/diskN as data dir
DISK=`df --output=pcent,target | grep /media/disk | sed "s/ /0/g" | sort | head -1 | sed "s/.*0\//\//"`
if [ -z "$DISK" ]; then
	echo "ERROR: could not find a disk to copy to"
	exit
fi

GS="python3 $HOME/pm-graph/googlesheet.py"
URL="http://otcpl-perf-data.jf.intel.com/pm-graph-test"
WEBDIR="$HOME/pm-graph-test"
SORTDIR="$HOME/pm-graph-sort"
DATADIR="$DISK/pm-graph-test"
MS="$HOME/.machswap"

$GS -webdir "$WEBDIR" -datadir "$DATADIR" -sortdir "$SORTDIR" -urlprefix "$URL" -machswap "$MS" -stype sheet -create both -bugzilla -parallel 0 -genhtml -cache -rmtar $1
