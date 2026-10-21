@{
    # The scripts report their findings on the console, so writing to it is intended.
    ExcludeRules = @('PSAvoidUsingWriteHost')
    Rules        = @{
        PSUseCompatibleSyntax = @{
            Enable         = $true
            TargetVersions = @('5.1', '7.4')
        }
    }
}
