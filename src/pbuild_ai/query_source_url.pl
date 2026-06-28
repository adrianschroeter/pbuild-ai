#!/usr/bin/perl

BEGIN {
  unshift @INC, '/usr/lib/build';
}

use strict;
use warnings;
use Build;
use Build::Rpm;

# Parse spec and print all Source: URLs with macros expanded
my $specfile = shift @ARGV or die("Usage: $0 <specfile>\n");
my $arch = shift @ARGV || 'noarch';

my $config = Build::read_config($arch, []);
local $Build::Rpm::includecallback = sub {
  my ($recipe, $file) = @_;
  $file =~ s/.*\///;
  $recipe =~ s/[^\/]+$//;
  $file = "$recipe$file";
  my $fd;
  my $str;
  if (open($fd, '<', $file)) { local $/; $str = <$fd>; close($fd); }
  return $str;
};

my $spec;
open(my $fh, '<', $specfile) or die("open: $!\n");
while (<$fh>) { push @$spec, $_; }
close($fh);

my $descr = Build::Rpm::parse($config, $spec);
die("unable to parse specfile: $specfile\n") unless $descr;

for my $key (sort keys %$descr) {
  if ($key =~ /^source\d*$/i && defined $descr->{$key}) {
    my $val = $descr->{$key};
    if ($val =~ /^(git\+)?https?:\/\//) {
      print "$key: $val\n";
    }
  }
}
